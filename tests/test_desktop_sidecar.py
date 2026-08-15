import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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
        block=False,
        timeline=None,
        error=None,
    ) -> None:
        self.approval = approval
        self.request_approval = request_approval
        from myagent.session_timeline import SessionTimeline
        self.session_timeline = timeline or SessionTimeline()
        self.close_calls = 0
        self.stream_callback = stream_callback
        self.stream_chunks = stream_chunks
        self.block = block
        self.started = threading.Event()
        self.release = threading.Event()
        self.error = error

    def run(self, prompt: str) -> str:
        self.started.set()
        self.session_timeline.record_user_input([{"role": "user", "content": prompt}])
        if self.stream_chunks is not None and self.stream_callback is not None:
            self.stream_callback("start", "")
            for chunk in self.stream_chunks:
                self.stream_callback("delta", chunk)
        if self.block and not self.release.wait(2):
            raise TimeoutError("test runtime timed out")
        if self.error is not None:
            raise self.error
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
        self.session_timeline.record_response_output(
            [{"role": "assistant", "content": answer}]
        )
        self.session_timeline.record_request_succeeded()
        return answer

    @property
    def history(self):
        return self.session_timeline.snapshot_history()

    def snapshot_timeline(self):
        return self.session_timeline.snapshot()

    def restore_timeline(self, snapshot):
        self.session_timeline.restore(snapshot)

    def reset(self) -> None:
        self.session_timeline.reset()

    def close(self) -> None:
        self.close_calls += 1


class RuntimeFactory:
    def __init__(self) -> None:
        self.next_request_approval = False
        self.next_stream_chunks = None
        self.next_block = False
        self.next_error = None
        self.calls: list[tuple[object, dict]] = []
        self.runtimes: list[FakeRuntime] = []

    def __call__(self, approval, **_kwargs):
        self.calls.append((approval, dict(_kwargs)))
        runtime = FakeRuntime(
            approval,
            request_approval=self.next_request_approval,
            stream_callback=_kwargs.get("stream_callback"),
            stream_chunks=self.next_stream_chunks,
            block=self.next_block,
            timeline=_kwargs.get("timeline"),
            error=self.next_error,
        )
        self.next_request_approval = False
        self.next_stream_chunks = None
        self.next_block = False
        self.next_error = None
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

    def test_settings_update_is_strict_and_rebuilds_idle_runtime_with_history(self) -> None:
        session_id = self.register_and_select()
        self.sidecar.dispatch(
            "session.submit", {"sessionId": session_id, "prompt": "first"}
        )
        self.pump_until(lambda: self.sidecar.snapshot()["sessions"][0]["state"] == "idle")
        first_history = list(self.factory.runtimes[0].history)
        values = dict(self.sidecar.snapshot()["settings"])
        values["maxToolRounds"] = 23
        result = self.sidecar.dispatch("settings.update", values)
        self.assertEqual(result["state"]["settings"]["maxToolRounds"], 23)
        self.assertEqual(self.factory.runtimes[0].close_calls, 1)
        self.sidecar.dispatch(
            "session.submit", {"sessionId": session_id, "prompt": "继续"}
        )
        self.pump_until(lambda: len(self.factory.runtimes) == 2)
        self.assertEqual(self.factory.runtimes[1].history[: len(first_history)], first_history)
        self.assertEqual(self.factory.calls[1][1]["settings"].max_tool_rounds, 23)
        new_session = self.sidecar.dispatch("session.new")["state"]["activeSessionId"]
        self.sidecar.dispatch(
            "session.select_model", {"sessionId": new_session, "name": "model-one"}
        )
        self.sidecar.dispatch(
            "session.submit", {"sessionId": new_session, "prompt": "new"}
        )
        self.pump_until(lambda: len(self.factory.runtimes) == 3)
        self.assertEqual(self.factory.calls[2][1]["settings"].public_dict(), values)
        invalid = dict(values)
        invalid["maxToolRounds"] = True
        with self.assertRaisesRegex(ProtocolError, "必须是整数"):
            self.sidecar.dispatch("settings.update", invalid)

    def test_busy_settings_update_does_not_save_or_partially_apply(self) -> None:
        session_id = self.register_and_select()
        self.factory.next_request_approval = True
        original = dict(self.sidecar.snapshot()["settings"])
        self.sidecar.dispatch(
            "session.submit", {"sessionId": session_id, "prompt": "wait"}
        )
        self.pump_until(lambda: any(name == "approval" for name, _ in self.events))
        proposed = dict(original)
        proposed["maxToolRounds"] = original["maxToolRounds"] + 1
        with self.assertRaisesRegex(ProtocolError, "暂不能保存"):
            self.sidecar.dispatch("settings.update", proposed)
        self.assertEqual(self.sidecar.snapshot()["settings"], original)
        self.assertFalse(self.sidecar.manager.settings_store.path.exists())
        approval = next(payload for name, payload in self.events if name == "approval")
        self.sidecar.dispatch(
            "approval.decide",
            {"approvalId": approval["approvalId"], "approved": False},
        )
        self.pump_until(lambda: self.sidecar.snapshot()["sessions"][0]["state"] == "idle")

    def test_runtime_close_failure_cannot_split_committed_settings(self) -> None:
        session_id = self.register_and_select()
        self.sidecar.dispatch(
            "session.submit", {"sessionId": session_id, "prompt": "first"}
        )
        self.pump_until(lambda: self.sidecar.snapshot()["sessions"][0]["state"] == "idle")
        self.factory.runtimes[0].close = MagicMock(
            side_effect=RuntimeError("close failed")
        )
        values = dict(self.sidecar.snapshot()["settings"])
        values["maxToolRounds"] += 1

        result = self.sidecar.dispatch("settings.update", values)

        self.assertEqual(result["state"]["settings"], values)
        self.assertEqual(self.sidecar.manager.settings.public_dict(), values)
        self.assertEqual(
            self.sidecar.manager.sessions[0].controller.agent._settings.public_dict(),
            values,
        )
        self.assertEqual(self.sidecar.manager.settings_store.load().public_dict(), values)
        self.factory.runtimes[0].close.assert_called_once_with()

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
        persisted = self.sidecar.manager.session_store.load_session(session_id)
        self.assertEqual([item["speaker"] for item in persisted.messages], ["你", "MyAgent"])
        self.assertEqual(persisted.timeline.snapshot_history()[-1]["role"], "assistant")

    def test_stream_chunks_do_not_commit_and_each_terminal_commits_once(self) -> None:
        session_id = self.register_and_select()
        path = self.sidecar.manager.session_store.root / f"{session_id}.jsonl"
        before = path.read_bytes()
        before_lines = path.read_text(encoding="utf-8").splitlines()
        self.factory.next_stream_chunks = ["a", "b"]
        self.factory.next_block = True
        self.sidecar.dispatch(
            "session.submit", {"sessionId": session_id, "prompt": "streamed"}
        )
        self.assertTrue(self.factory.runtimes[-1].started.wait(1))
        self.sidecar.poll_once()
        self.assertEqual(path.read_bytes(), before)
        self.factory.runtimes[-1].release.set()
        self.pump_until(lambda: self.sidecar.manager.sessions[0].controller.state == "idle")
        after_assistant = path.read_text(encoding="utf-8").splitlines()
        appended = [json.loads(line)["event"] for line in after_assistant[len(before_lines):]]
        self.assertEqual(appended, ["turn_committed"])
        self.sidecar.poll_once()
        self.assertEqual(path.read_text(encoding="utf-8").splitlines(), after_assistant)

        second_id = self.sidecar.dispatch("session.new")["state"]["activeSessionId"]
        self.sidecar.dispatch(
            "session.configure_agent_model",
            {"sessionId": second_id, "role": "main", "name": "model-one"},
        )
        second_path = self.sidecar.manager.session_store.root / f"{second_id}.jsonl"
        second_before = second_path.read_text(encoding="utf-8").splitlines()
        self.factory.next_error = RuntimeError("terminal error")
        self.sidecar.dispatch(
            "session.submit", {"sessionId": second_id, "prompt": "fails"}
        )
        self.pump_until(
            lambda: next(
                item for item in self.sidecar.snapshot()["sessions"] if item["id"] == second_id
            )["state"] == "idle"
        )
        second_after = second_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            [json.loads(line)["event"] for line in second_after[len(second_before):]],
            ["turn_committed"],
        )
        self.sidecar.poll_once()
        self.assertEqual(second_path.read_text(encoding="utf-8").splitlines(), second_after)

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

    def test_session_delete_removes_only_the_target_and_is_persistent(self) -> None:
        first_id = self.sidecar.snapshot()["activeSessionId"]
        second = self.sidecar.dispatch("session.new")["state"]["activeSessionId"]
        result = self.sidecar.dispatch("session.delete", {"sessionId": second})
        self.assertEqual([item["id"] for item in result["state"]["sessions"]], [first_id])
        self.assertEqual(result["state"]["activeSessionId"], first_id)
        with self.assertRaisesRegex(ProtocolError, "至少保留"):
            self.sidecar.dispatch("session.delete", {"sessionId": first_id})
        restored = SessionManager(
            store=ModelStore(self.sidecar.manager.store.path),
            default_cwd=self.temp.name,
            runtime_factory=RuntimeFactory(),
        )
        self.addCleanup(restored.begin_close_all)
        self.assertEqual([item.session_id for item in restored.sessions], [first_id])

    def test_two_busy_shutdown_terminals_both_persist_and_restore(self) -> None:
        first_id = self.register_and_select()
        second_id = self.sidecar.dispatch("session.new")["state"]["activeSessionId"]
        self.sidecar.dispatch(
            "session.configure_agent_model",
            {"sessionId": second_id, "role": "main", "name": "model-one"},
        )
        for session_id in (first_id, second_id):
            self.factory.next_block = True
            self.sidecar.dispatch(
                "session.submit", {"sessionId": session_id, "prompt": f"turn-{session_id}"}
            )
            self.assertTrue(self.factory.runtimes[-1].started.wait(1))
        self.sidecar.dispatch("app.shutdown")
        self.factory.runtimes[0].release.set()
        self.pump_until(
            lambda: self.sidecar.manager.sessions[0].controller.state == "closed"
        )
        self.factory.runtimes[1].release.set()
        self.pump_until(self.sidecar.manager.all_closed)
        persisted = [
            self.sidecar.manager.session_store.load_session(session_id)
            for session_id in (first_id, second_id)
        ]
        self.assertEqual([len(item.messages) for item in persisted], [2, 2])
        self.assertEqual([len(item.timeline.snapshot_history()) for item in persisted], [2, 2])
        restored = SessionManager(
            store=ModelStore(self.sidecar.manager.store.path),
            default_cwd=self.temp.name,
            runtime_factory=RuntimeFactory(),
        )
        self.addCleanup(restored.begin_close_all)
        self.assertEqual([len(item.messages) for item in restored.sessions], [2, 2])

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
