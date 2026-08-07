import json
import tempfile
import time
import unittest
from pathlib import Path

from myagent.desktop_sidecar import DesktopSidecar, ProtocolError
from myagent.gui import SessionManager
from myagent.gui_models import ModelStore
from myagent.permissions import ApprovalRequest


class FakeRuntime:
    def __init__(
        self,
        approval,
        *,
        request_approval: bool = False,
        stream_callback=None,
        stream_chunks=None,
    ) -> None:
        self.approval = approval
        self.request_approval = request_approval
        self.history = []
        self.close_calls = 0
        self.stream_callback = stream_callback
        self.stream_chunks = stream_chunks

    def run(self, prompt: str) -> str:
        self.history.append({"role": "user", "content": prompt})
        if self.stream_chunks is not None and self.stream_callback is not None:
            self.stream_callback("start", "")
            for chunk in self.stream_chunks:
                self.stream_callback("delta", chunk)
        if self.request_approval:
            approved = self.approval(
                ApprovalRequest(
                    "bash",
                    {"command": "inspect", "token": "sidecar-secret"},
                    "reason contains sidecar-secret",
                )
            )
            answer = "已允许" if approved else "已拒绝"
        else:
            answer = f"回答：{prompt}"
        self.history.append({"role": "assistant", "content": answer})
        return answer

    def reset(self) -> None:
        self.history.clear()

    def close(self) -> None:
        self.close_calls += 1


class RuntimeFactory:
    def __init__(self) -> None:
        self.next_request_approval = False
        self.next_stream_chunks = None
        self.calls: list[tuple[object, dict]] = []
        self.runtimes: list[FakeRuntime] = []

    def __call__(self, approval, **_kwargs):
        self.calls.append((approval, dict(_kwargs)))
        runtime = FakeRuntime(
            approval,
            request_approval=self.next_request_approval,
            stream_callback=_kwargs.get("stream_callback"),
            stream_chunks=self.next_stream_chunks,
        )
        self.next_request_approval = False
        self.next_stream_chunks = None
        self.runtimes.append(runtime)
        return runtime


class DesktopSidecarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.factory = RuntimeFactory()
        manager = SessionManager(
            store=ModelStore(Path(self.temp.name) / "models.json"),
            default_cwd=self.temp.name,
            runtime_factory=self.factory,
        )
        self.events: list[tuple[str, dict]] = []
        self.sidecar = DesktopSidecar(
            manager,
            emit=lambda name, payload: self.events.append((name, payload)),
            start_polling=False,
        )
        self.addCleanup(self.sidecar.close)

    def pump_until(self, predicate, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.sidecar.poll_once()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("desktop sidecar did not reach the expected state")

    def register_and_select(self) -> int:
        self.sidecar.dispatch(
            "model.register",
            {
                "name": "model-one",
                "baseUrl": "https://gateway.example/v1",
                "apiKey": "sidecar-secret",
            },
        )
        session_id = self.sidecar.snapshot()["activeSessionId"]
        self.sidecar.dispatch(
            "session.select_model",
            {"sessionId": session_id, "name": "model-one"},
        )
        return session_id

    def test_snapshot_never_exposes_registered_api_key(self) -> None:
        session_id = self.register_and_select()
        snapshot = self.sidecar.snapshot()
        serialized = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("sidecar-secret", serialized)
        self.assertNotIn("apiKey", serialized)
        self.assertEqual(snapshot["activeSessionId"], session_id)
        self.assertEqual(snapshot["models"][0]["name"], "model-one")
        self.assertTrue(snapshot["sessions"][0]["canChangeModel"])
        self.assertTrue(snapshot["sessions"][0]["canConfigureAgents"])
        self.assertEqual(
            snapshot["sessions"][0]["agentModels"],
            {"main": "model-one", "sub": ""},
        )
        self.assertEqual(
            snapshot["models"][0]["baseUrl"],
            "https://gateway.example/v1",
        )

    def test_submit_reuses_headless_session_runtime_and_publishes_state(self) -> None:
        session_id = self.register_and_select()
        result = self.sidecar.dispatch(
            "session.submit",
            {"sessionId": session_id, "prompt": "你好"},
        )
        self.assertEqual(result["state"]["sessions"][0]["messages"][0]["speaker"], "你")
        self.assertFalse(result["state"]["sessions"][0]["canChangeModel"])
        self.assertFalse(result["state"]["sessions"][0]["canConfigureAgents"])
        self.pump_until(
            lambda: len(self.sidecar.snapshot()["sessions"][0]["messages"]) == 2
        )
        session = self.sidecar.snapshot()["sessions"][0]
        self.assertEqual(session["messages"][-1], {"speaker": "MyAgent", "text": "回答：你好"})
        self.assertEqual(session["state"], "idle")
        self.assertTrue(session["canChangeModel"])
        self.assertTrue(session["canConfigureAgents"])
        self.assertTrue(any(name == "state" for name, _payload in self.events))

    def test_main_and_subagent_models_are_configured_independently(self) -> None:
        session_id = self.register_and_select()
        self.sidecar.dispatch(
            "model.register",
            {
                "name": "model-child",
                "baseUrl": "https://child.example/v1",
                "apiKey": "child-secret",
            },
        )
        result = self.sidecar.dispatch(
            "session.configure_agent_model",
            {"sessionId": session_id, "role": "sub", "name": "model-child"},
        )
        self.assertEqual(
            result["state"]["sessions"][0]["agentModels"],
            {"main": "model-one", "sub": "model-child"},
        )

        self.sidecar.dispatch(
            "session.submit",
            {"sessionId": session_id, "prompt": "使用子 Agent"},
        )
        self.pump_until(
            lambda: self.sidecar.snapshot()["sessions"][0]["state"] == "idle"
        )
        runtime_options = self.factory.calls[0][1]
        self.assertEqual(runtime_options["model"], "model-one")
        self.assertEqual(runtime_options["subagent_model"], "model-child")
        self.assertEqual(runtime_options["subagent_api_key"], "child-secret")
        self.assertEqual(
            runtime_options["subagent_base_url"],
            "https://child.example/v1",
        )

        cleared = self.sidecar.dispatch(
            "session.configure_agent_model",
            {"sessionId": session_id, "role": "sub", "name": ""},
        )
        self.assertEqual(cleared["state"]["sessions"][0]["agentModels"]["sub"], "")

    def test_session_title_can_be_renamed_through_protocol(self) -> None:
        session_id = self.sidecar.snapshot()["activeSessionId"]
        result = self.sidecar.dispatch(
            "session.rename",
            {"sessionId": session_id, "title": "  发布   排查  "},
        )
        self.assertEqual(result["state"]["sessions"][0]["title"], "发布 排查")
        with self.assertRaisesRegex(ProtocolError, "must not be empty"):
            self.sidecar.dispatch(
                "session.rename",
                {"sessionId": session_id, "title": "   "},
            )
        with self.assertRaisesRegex(ProtocolError, "80"):
            self.sidecar.dispatch(
                "session.rename",
                {"sessionId": session_id, "title": "x" * 81},
            )

    def test_stream_events_precede_final_state_and_never_expose_split_key(self) -> None:
        session_id = self.register_and_select()
        self.factory.next_stream_chunks = ["流式：", "sidecar-", "secret"]
        event_start = len(self.events)

        self.sidecar.dispatch(
            "session.submit",
            {"sessionId": session_id, "prompt": "你好"},
        )
        self.pump_until(
            lambda: self.sidecar.snapshot()["sessions"][0]["state"] == "idle"
        )

        turn_events = self.events[event_start:]
        names = [name for name, _payload in turn_events]
        self.assertIn("stream-start", names)
        self.assertIn("stream-delta", names)
        self.assertLess(names.index("stream-start"), names.index("state"))
        streamed = "".join(
            payload["delta"]
            for name, payload in turn_events
            if name == "stream-delta"
        )
        self.assertEqual(streamed, "流式：[redacted]")
        self.assertNotIn(
            "sidecar-secret",
            json.dumps(turn_events, ensure_ascii=False),
        )
        self.assertEqual(
            self.sidecar.snapshot()["sessions"][0]["messages"][-1]["text"],
            "回答：你好",
        )

    def test_approval_payload_is_redacted_and_decision_unblocks_worker(self) -> None:
        session_id = self.register_and_select()
        self.factory.next_request_approval = True
        self.sidecar.dispatch(
            "session.submit",
            {"sessionId": session_id, "prompt": "执行检查"},
        )
        self.pump_until(lambda: any(name == "approval" for name, _ in self.events))
        approval = next(payload for name, payload in self.events if name == "approval")
        self.assertNotIn("sidecar-secret", json.dumps(approval, ensure_ascii=False))
        self.assertIn("[redacted]", approval["reason"])
        self.sidecar.dispatch(
            "approval.decide",
            {"approvalId": approval["approvalId"], "approved": True},
        )
        self.pump_until(
            lambda: self.sidecar.snapshot()["sessions"][0]["state"] == "idle"
        )
        self.assertEqual(
            self.sidecar.snapshot()["sessions"][0]["messages"][-1]["text"],
            "已允许",
        )

    def test_protocol_rejects_unknown_methods_and_invalid_params(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "unknown method"):
            self.sidecar.dispatch("system.shell", {})
        with self.assertRaisesRegex(ProtocolError, "params must be an object"):
            self.sidecar.dispatch("app.snapshot", [])
        with self.assertRaisesRegex(ProtocolError, "sessionId must be an integer"):
            self.sidecar.dispatch("session.activate", {"sessionId": True})
        with self.assertRaisesRegex(ProtocolError, "baseUrl must be a string"):
            self.sidecar.dispatch(
                "model.register",
                {"name": "bad", "apiKey": "secret", "baseUrl": 7},
            )


if __name__ == "__main__":
    unittest.main()
