import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import tkinter as tk

import customtkinter as ctk

from myagent.gui import (
    MAIN_AGENT_LABEL,
    SUBAGENT_LABEL,
    ApprovalBridge,
    ApprovalProposal,
    ConversationController,
    DeferredAgent,
    MyAgentWindow,
    SessionManager,
    UIEvent,
    _set_windows_app_id,
    create_runtime,
    main,
)
from myagent.gui_models import ModelRecord, ModelStore
from myagent.gui_conversations import ConversationStoreError
from myagent.permissions import ApprovalRequest


class FakeRuntime:
    def __init__(
        self,
        result="回答",
        *,
        block=False,
        stream_callback=None,
        stream_chunks=None,
    ) -> None:
        self.result = result
        self.block = block
        self.started = threading.Event()
        self.release = threading.Event()
        self.history = []
        self.run_calls = []
        self.close_calls = 0
        self.reset_calls = 0
        self.stream_callback = stream_callback
        self.stream_chunks = stream_chunks

    def run(self, prompt: str) -> str:
        self.started.set()
        self.run_calls.append(prompt)
        self.history.append({"role": "user", "content": prompt})
        if self.stream_chunks is not None and self.stream_callback is not None:
            self.stream_callback("start", "")
            for chunk in self.stream_chunks:
                self.stream_callback("delta", chunk)
        if self.block and not self.release.wait(2):
            raise TimeoutError("test runtime timed out")
        if isinstance(self.result, BaseException):
            raise self.result
        self.history.append({"role": "assistant", "content": self.result})
        return self.result

    def reset(self) -> None:
        self.reset_calls += 1
        self.history.clear()

    def close(self) -> None:
        self.close_calls += 1


class RuntimeFactory:
    def __init__(self) -> None:
        self.calls = []
        self.runtimes = []
        self.next_result = "回答"
        self.next_block = False
        self.next_stream_chunks = None

    def __call__(self, approval, **kwargs):
        self.calls.append((approval, kwargs))
        runtime = FakeRuntime(
            self.next_result,
            block=self.next_block,
            stream_callback=kwargs.get("stream_callback"),
            stream_chunks=self.next_stream_chunks,
        )
        self.runtimes.append(runtime)
        self.next_block = False
        self.next_stream_chunks = None
        return runtime


def next_result(controller: ConversationController, timeout: float = 2) -> UIEvent:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = controller.events.get(timeout=max(0.01, deadline - time.monotonic()))
        if event.kind != "approval":
            return event
    raise AssertionError("no result event")


def consume(controller: ConversationController) -> UIEvent:
    event = next_result(controller)
    controller.consume_result()
    return event


class DeferredAgentTests(unittest.TestCase):
    def test_lazy_runtime_carries_secret_workspace_kind_and_safe_repr(self) -> None:
        factory = RuntimeFactory()
        approval = MagicMock(return_value=False)
        with tempfile.TemporaryDirectory() as folder:
            agent = DeferredAgent(
                approval,
                model="model-a",
                api_key="sk-secret-value",
                base_url="https://gateway.example/v1",
                subagent_model="model-child",
                subagent_api_key="sk-child-secret",
                subagent_base_url="https://child.example/v1",
                cwd=folder,
                kind="sub",
                runtime_factory=factory,
            )
            self.assertFalse(agent.loaded)
            self.assertNotIn("sk-secret-value", repr(agent))
            self.assertEqual(agent.run("你好"), "回答")
            self.assertTrue(agent.loaded)
            kwargs = factory.calls[0][1]
            self.assertEqual(kwargs["model"], "model-a")
            self.assertEqual(kwargs["api_key"], "sk-secret-value")
            self.assertEqual(kwargs["base_url"], "https://gateway.example/v1")
            self.assertEqual(kwargs["subagent_model"], "model-child")
            self.assertEqual(kwargs["subagent_api_key"], "sk-child-secret")
            self.assertEqual(kwargs["subagent_base_url"], "https://child.example/v1")
            self.assertEqual(kwargs["cwd"], Path(folder).resolve())
            self.assertEqual(kwargs["kind"], "sub")
            agent.close()
            agent.close()
            self.assertEqual(factory.runtimes[0].close_calls, 1)

    def test_idle_model_switch_rebuilds_client_and_preserves_history(self) -> None:
        factory = RuntimeFactory()
        agent = DeferredAgent(
            lambda request: False,
            model="one",
            api_key="key-one",
            runtime_factory=factory,
        )
        self.assertEqual(agent.run("first"), "回答")
        first_history = list(factory.runtimes[0].history)

        agent.configure_model("two", "key-two", "https://second.example/v1")
        self.assertEqual(factory.runtimes[0].close_calls, 1)
        self.assertEqual(agent.run("second"), "回答")

        self.assertEqual(factory.calls[1][1]["api_key"], "key-two")
        self.assertEqual(
            factory.calls[1][1]["base_url"],
            "https://second.example/v1",
        )
        self.assertEqual(factory.runtimes[1].history[: len(first_history)], first_history)
        agent.close()

    def test_cli_style_system_exit_becomes_a_normal_runtime_error(self) -> None:
        def failing_factory(approval, **kwargs):
            raise SystemExit("invalid config")

        agent = DeferredAgent(
            lambda request: False,
            model="one",
            api_key="secret",
            runtime_factory=failing_factory,
        )
        with self.assertRaisesRegex(RuntimeError, "invalid config"):
            agent.run("hello")
        agent.close()


class ConversationControllerTests(unittest.TestCase):
    def make_controller(self, factory=None):
        factory = factory or RuntimeFactory()
        events = queue.Queue()
        approvals = ApprovalBridge(events)
        agent = DeferredAgent(approvals.request, runtime_factory=factory)
        controller = ConversationController(agent, events=events, approvals=approvals)
        return controller, factory

    def tearDownController(self, controller, factory):
        for runtime in factory.runtimes:
            runtime.release.set()
        if controller.state == "busy":
            try:
                next_result(controller)
                controller.consume_result()
            except (queue.Empty, RuntimeError):
                pass
        if controller.state not in {"closing", "closed"}:
            if controller.request_close():
                controller.close_agent_once()
        elif controller.state == "closing":
            controller.close_agent_once()

    def test_no_model_blocks_send_and_kind_locks_after_first_send(self) -> None:
        controller, factory = self.make_controller()
        self.addCleanup(self.tearDownController, controller, factory)
        self.assertFalse(controller.submit("hello"))
        self.assertIn("注册并选择模型", controller.last_error)
        self.assertTrue(controller.set_kind("sub"))
        self.assertEqual(controller.kind, "sub")
        self.assertTrue(controller.set_model(ModelRecord("m", "secret", "2026-01-01T00:00:00Z")))
        self.assertTrue(controller.submit("hello"))
        consume(controller)
        self.assertFalse(controller.set_kind("main"))
        self.assertIn("新建对话", controller.last_error)
        self.assertEqual(controller.kind, "sub")

    def test_busy_extends_until_main_thread_consumes_result(self) -> None:
        controller, factory = self.make_controller()
        self.addCleanup(self.tearDownController, controller, factory)
        controller.set_model(ModelRecord("m", "secret", "2026-01-01T00:00:00Z"))
        self.assertTrue(controller.submit("one"))
        event = next_result(controller)
        self.assertEqual(event.payload, "回答")
        self.assertEqual(controller.state, "busy")
        self.assertFalse(controller.submit("too early"))
        self.assertFalse(controller.consume_result())
        self.assertEqual(controller.state, "idle")

    def test_model_can_switch_after_a_completed_turn_and_preserve_history(self) -> None:
        controller, factory = self.make_controller()
        self.addCleanup(self.tearDownController, controller, factory)
        first = ModelRecord("one", "key-one", "2026-01-01T00:00:00Z")
        second = ModelRecord("two", "key-two", "2026-01-02T00:00:00Z")
        self.assertTrue(controller.set_model(first))
        self.assertTrue(controller.submit("first"))
        consume(controller)
        first_history = list(factory.runtimes[0].history)

        self.assertTrue(controller.has_started)
        self.assertTrue(controller.set_model(second))
        self.assertEqual(controller.model, "two")
        self.assertEqual(factory.runtimes[0].close_calls, 1)
        self.assertTrue(controller.submit("second"))
        consume(controller)

        self.assertEqual(factory.calls[1][1]["model"], "two")
        self.assertEqual(factory.runtimes[1].history[: len(first_history)], first_history)

    def test_subagent_model_can_change_after_a_completed_turn(self) -> None:
        controller, factory = self.make_controller()
        self.addCleanup(self.tearDownController, controller, factory)
        main = ModelRecord("main", "main-key", "2026-01-01T00:00:00Z")
        child = ModelRecord(
            "child",
            "child-key",
            "2026-01-02T00:00:00Z",
            "https://child.example/v1",
        )
        self.assertTrue(controller.set_agent_model("main", main))
        self.assertTrue(controller.submit("first"))
        consume(controller)
        first_history = list(factory.runtimes[0].history)

        self.assertTrue(controller.set_agent_model("sub", child))
        self.assertEqual(controller.subagent_model, "child")
        self.assertEqual(factory.runtimes[0].close_calls, 1)
        self.assertTrue(controller.submit("second"))
        consume(controller)

        options = factory.calls[1][1]
        self.assertEqual(options["model"], "main")
        self.assertEqual(options["subagent_model"], "child")
        self.assertEqual(options["subagent_api_key"], "child-key")
        self.assertEqual(factory.runtimes[1].history[: len(first_history)], first_history)

    def test_stream_deltas_arrive_before_final_result_and_redact_split_secret(self) -> None:
        factory = RuntimeFactory()
        factory.next_result = "最终回答"
        factory.next_stream_chunks = ["流式：", "super-", "secret", "。"]
        controller, factory = self.make_controller(factory)
        self.addCleanup(self.tearDownController, controller, factory)
        controller.set_model(
            ModelRecord("m", "super-secret", "2026-01-01T00:00:00Z")
        )

        self.assertTrue(controller.submit("one"))
        events = []
        while not events or events[-1].kind != "assistant":
            events.append(controller.events.get(timeout=2))

        self.assertEqual(events[0].kind, "stream_start")
        streamed = "".join(
            str(event.payload) for event in events if event.kind == "stream_delta"
        )
        self.assertEqual(streamed, "流式：[redacted]。")
        self.assertNotIn("super-secret", repr(events))
        self.assertEqual(events[-1].payload, "最终回答")
        self.assertEqual(controller.state, "busy")
        self.assertFalse(controller.consume_result())

    def test_busy_application_close_waits_then_closes_runtime_once(self) -> None:
        factory = RuntimeFactory()
        factory.next_block = True
        controller, factory = self.make_controller(factory)
        controller.set_model(ModelRecord("m", "secret", "2026-01-01T00:00:00Z"))
        self.assertTrue(controller.submit("wait"))
        self.assertTrue(factory.runtimes[0].started.wait(1))
        self.assertFalse(controller.request_close())
        self.assertEqual(controller.state, "closing")
        factory.runtimes[0].release.set()
        next_result(controller)
        self.assertTrue(controller.consume_result())
        controller.close_agent_once()
        controller.close_agent_once()
        self.assertEqual(factory.runtimes[0].close_calls, 1)

    def test_worker_errors_redact_key_and_close_denies_approvals(self) -> None:
        factory = RuntimeFactory()
        factory.next_result = RuntimeError("authentication rejected super-secret")
        controller, factory = self.make_controller(factory)
        controller.set_model(
            ModelRecord("m", "super-secret", "2026-01-01T00:00:00Z")
        )
        self.assertTrue(controller.submit("fail"))
        event = consume(controller)
        self.assertEqual(event.kind, "error")
        self.assertNotIn("super-secret", str(event.payload))
        self.assertIn("[redacted]", str(event.payload))

        request = ApprovalRequest("bash", {"command": "rm note"}, "delete")
        result = []
        worker = threading.Thread(
            target=lambda: result.append(controller.approvals.request(request)),
            daemon=True,
        )
        worker.start()
        proposal_event = controller.events.get(timeout=1)
        self.assertIsInstance(proposal_event.payload, ApprovalProposal)
        self.assertTrue(controller.request_close())
        worker.join(1)
        self.assertEqual(result, [False])
        controller.close_agent_once()
        controller.close_agent_once()
        self.assertEqual(factory.runtimes[0].close_calls, 1)

    def test_success_event_redacts_key_and_empty_sensitive_values_are_safe(self) -> None:
        factory = RuntimeFactory()
        factory.next_result = "answer accidentally contains success-secret"
        controller, factory = self.make_controller(factory)
        self.addCleanup(self.tearDownController, controller, factory)
        self.assertEqual(controller.redact_for_ui("plain text"), "plain text")
        controller.set_model(
            ModelRecord("m", "success-secret", "2026-01-01T00:00:00Z")
        )
        self.assertTrue(controller.submit("hello"))
        event = next_result(controller)
        self.assertEqual(event.kind, "assistant")
        self.assertNotIn("success-secret", str(event.payload))
        self.assertIn("[redacted]", str(event.payload))
        controller.consume_result()


class SessionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace_a = self.root / "a"
        self.workspace_b = self.root / "b"
        self.workspace_a.mkdir()
        self.workspace_b.mkdir()
        self.factory = RuntimeFactory()
        self.store = ModelStore(self.root / "config" / "models.json")
        self.manager = SessionManager(
            store=self.store,
            default_cwd=self.workspace_a,
            runtime_factory=self.factory,
        )
        self.addCleanup(self._close_manager)

    def _close_manager(self) -> None:
        for runtime in self.factory.runtimes:
            runtime.release.set()
        for session in self.manager.sessions:
            if session.controller.state == "busy":
                try:
                    next_result(session.controller)
                    session.controller.consume_result()
                except (queue.Empty, RuntimeError):
                    pass
        self.manager.begin_close_all()

    def test_new_open_cancel_and_workspace_ownership(self) -> None:
        process_cwd = Path.cwd()
        initial = self.manager.active
        self.assertEqual(initial.workspace, self.workspace_a.resolve())
        before = list(self.manager.sessions)
        self.assertIsNone(self.manager.open_folder(""))
        self.assertEqual(self.manager.sessions, before)

        second = self.manager.open_folder(self.workspace_b)
        self.assertIsNotNone(second)
        self.assertEqual(second.workspace, self.workspace_b.resolve())
        self.assertIsNot(initial.controller, second.controller)
        self.assertEqual(self.manager.active_session_id, second.session_id)
        self.assertEqual(Path.cwd(), process_cwd)

    def test_corrupt_model_registry_remains_user_visible_at_startup(self) -> None:
        corrupt_path = self.root / "broken" / "models.json"
        corrupt_path.parent.mkdir()
        corrupt_path.write_text("{broken", encoding="utf-8")
        manager = SessionManager(
            store=ModelStore(corrupt_path),
            default_cwd=self.workspace_a,
            runtime_factory=self.factory,
        )
        self.assertIn("JSON 已损坏", manager.model_error)
        manager.begin_close_all()

    def test_tabs_keep_model_kind_and_history_independent(self) -> None:
        self.manager.register_model("one", "key-one", "https://one.example/v1")
        self.manager.register_model("two", "key-two")
        first = self.manager.active
        second = self.manager.new_session(self.workspace_b)
        self.assertTrue(self.manager.select_model(first.session_id, "one"))
        self.assertTrue(self.manager.select_model(second.session_id, "two"))
        self.assertTrue(second.controller.set_kind("sub"))
        self.assertEqual(first.controller.model, "one")
        self.assertEqual(first.controller.kind, "main")
        self.assertEqual(second.controller.model, "two")
        self.assertEqual(second.controller.kind, "sub")
        self.assertIsNot(first.controller.events, second.controller.events)
        self.assertTrue(first.controller.submit("first"))
        consume(first.controller)
        self.assertEqual(
            self.factory.calls[0][1]["base_url"],
            "https://one.example/v1",
        )

    def test_idle_close_once_busy_close_blocked_and_one_tab_remains(self) -> None:
        self.manager.register_model("one", "key-one")
        first = self.manager.active
        second = self.manager.new_session(self.workspace_b)
        self.manager.select_model(second.session_id, "one")
        self.factory.next_block = True
        self.assertTrue(second.controller.submit("wait"))
        self.assertTrue(self.factory.runtimes[0].started.wait(1))
        self.assertFalse(self.manager.close_session(second.session_id))
        self.assertIn("正在运行", self.manager.last_error)
        self.factory.runtimes[0].release.set()
        consume(second.controller)
        self.assertTrue(self.manager.close_session(second.session_id))
        self.assertEqual(self.factory.runtimes[0].close_calls, 1)
        self.assertEqual(self.manager.sessions, [first])
        self.assertFalse(self.manager.close_session(first.session_id))
        self.assertEqual(len(self.manager.sessions), 1)

    def test_session_title_can_be_renamed_with_bounded_normalization(self) -> None:
        session = self.manager.active
        self.assertTrue(self.manager.rename_session(session.session_id, "  项目   排查  "))
        self.assertEqual(session.title, "项目 排查")
        self.assertFalse(self.manager.rename_session(session.session_id, "   "))
        self.assertEqual(session.title, "项目 排查")
        self.assertIn("不能为空", self.manager.last_error)
        self.assertFalse(self.manager.rename_session(session.session_id, "x" * 81))
        self.assertIn("80", self.manager.last_error)

    def test_sessions_restore_order_active_models_kind_messages_and_history(self) -> None:
        self.manager.register_model("one", "key-one", "https://one.example/v1")
        self.manager.register_model("two", "key-two")
        first = self.manager.active
        self.assertTrue(self.manager.select_model(first.session_id, "one"))
        first_history = [
            {"role": "user", "content": "first"},
            {"type": "reasoning", "id": "reason-1", "summary": []},
            {"type": "function_call", "call_id": "call-1", "name": "inspect"},
            {"type": "function_call_output", "call_id": "call-1", "output": "ok"},
        ]
        first.controller.agent.restore_history(first_history)
        first.messages.extend([("你", "first"), ("MyAgent", "done")])
        self.assertTrue(self.manager.persist_completed_turn(first.session_id))

        second = self.manager.new_session(self.workspace_b)
        self.assertTrue(self.manager.select_model(second.session_id, "two"))
        self.assertTrue(self.manager.set_kind(second.session_id, "sub"))
        second.controller.agent.restore_history([{"role": "user", "content": "second"}])
        second.messages.append(("你", "second"))
        self.assertTrue(self.manager.persist_completed_turn(second.session_id))
        self.assertTrue(self.manager.rename_session(first.session_id, "第一段对话"))
        self.assertTrue(self.manager.activate(first.session_id))

        factory = RuntimeFactory()
        restored = SessionManager(
            store=self.store,
            default_cwd=self.workspace_a,
            runtime_factory=factory,
        )
        self.addCleanup(restored.begin_close_all)
        self.assertEqual([item.session_id for item in restored.sessions], [1, 2])
        self.assertEqual(restored.active_session_id, 1)
        restored_first, restored_second = restored.sessions
        self.assertEqual(restored_first.title, "第一段对话")
        self.assertEqual(restored_first.controller.model, "one")
        self.assertEqual(restored_second.controller.model, "two")
        self.assertEqual(restored_second.controller.kind, "sub")
        self.assertEqual(restored_first.messages[-1], ("MyAgent", "done"))
        self.assertEqual(restored_first.controller.agent.snapshot_history(), first_history)
        self.assertTrue(restored_first.controller.has_started)
        self.assertFalse(restored_first.controller.set_kind("sub"))
        self.assertTrue(restored_first.controller.submit("continue"))
        consume(restored_first.controller)
        self.assertEqual(factory.runtimes[0].history[: len(first_history)], first_history)
        self.assertEqual(restored.new_session(self.workspace_a).session_id, 3)
        serialized = restored.conversation_store.path.read_text(encoding="utf-8")
        self.assertNotIn("key-one", serialized)
        self.assertNotIn("key-two", serialized)

    def test_delete_store_failure_keeps_memory_and_runtime_owner(self) -> None:
        first = self.manager.active
        second = self.manager.new_session(self.workspace_b)
        with patch.object(
            self.manager.conversation_store,
            "save",
            side_effect=ConversationStoreError("disk unavailable"),
        ):
            self.assertFalse(self.manager.close_session(second.session_id))
        self.assertEqual(self.manager.sessions, [first, second])
        self.assertEqual(second.controller.state, "idle")
        self.assertIn("disk unavailable", self.manager.conversation_error)

    def test_busy_turn_is_not_committed_by_an_unrelated_session_change(self) -> None:
        self.manager.register_model("one", "key-one")
        first = self.manager.active
        self.assertTrue(self.manager.select_model(first.session_id, "one"))
        self.factory.next_block = True
        self.assertTrue(first.controller.submit("unfinished"))
        self.assertTrue(self.factory.runtimes[0].started.wait(1))
        first.messages.append(("你", "unfinished"))

        self.manager.new_session(self.workspace_b)
        saved_first = self.manager.conversation_store.load()["sessions"][0]
        self.assertEqual(saved_first["messages"], [])
        self.assertEqual(saved_first["history"], [])

        self.factory.runtimes[0].release.set()
        consume(first.controller)

    def test_conversation_rejects_all_registered_keys_and_base_urls(self) -> None:
        self.manager.register_model("selected", "selected-key", "https://selected.test/v1")
        self.manager.register_model("other", "other-key", "https://other.test/v1")
        session = self.manager.active
        self.assertTrue(self.manager.select_model(session.session_id, "selected"))
        secrets = "selected-key https://selected.test/v1 other-key https://other.test/v1"
        session.messages.append(("你", secrets))
        session.controller.agent.restore_history([{"role": "user", "content": secrets}])
        self.assertFalse(self.manager.persist_completed_turn(session.session_id))
        serialized = self.manager.conversation_store.path.read_text(encoding="utf-8")
        for secret in (
            "selected-key", "https://selected.test/v1",
            "other-key", "https://other.test/v1",
        ):
            self.assertNotIn(secret, serialized)

    def test_model_save_failure_keeps_loaded_main_and_sub_runtime(self) -> None:
        self.manager.register_model("one", "key-one")
        self.manager.register_model("two", "key-two")
        session = self.manager.active
        self.assertTrue(self.manager.select_model(session.session_id, "one"))
        self.assertTrue(session.controller.submit("first"))
        consume(session.controller)
        runtime = self.factory.runtimes[0]
        history = list(runtime.history)
        with patch.object(
            self.manager.conversation_store,
            "save",
            side_effect=ConversationStoreError("disk unavailable"),
        ):
            self.assertFalse(self.manager.select_model(session.session_id, "two"))
            self.assertFalse(
                self.manager.configure_agent_model(session.session_id, "sub", "two")
            )
        self.assertEqual(session.controller.model, "one")
        self.assertEqual(session.controller.subagent_model, "")
        self.assertTrue(session.controller.agent.loaded)
        self.assertEqual(runtime.history, history)
        self.assertEqual(runtime.close_calls, 0)

    def test_closing_terminal_is_persisted_before_runtime_close(self) -> None:
        self.manager.register_model("one", "key-one")
        session = self.manager.active
        self.assertTrue(self.manager.select_model(session.session_id, "one"))
        self.factory.next_block = True
        self.assertTrue(session.controller.submit("finish during shutdown"))
        session.messages.append(("你", "finish during shutdown"))
        self.assertTrue(self.factory.runtimes[0].started.wait(1))
        self.assertFalse(self.manager.begin_close_all())
        self.factory.runtimes[0].release.set()
        event = next_result(session.controller)
        self.assertTrue(session.controller.consume_result())
        session.messages.append(("MyAgent", str(event.payload)))
        self.assertTrue(self.manager.persist_completed_turn(session.session_id))
        session.controller.close_agent_once()
        persisted = self.manager.conversation_store.load()["sessions"][0]
        self.assertEqual(len(persisted["messages"]), 2)
        self.assertEqual(persisted["history"][-1]["role"], "assistant")

    def test_two_closing_terminals_survive_sequential_saves_and_restart(self) -> None:
        self.manager.register_model("one", "key-one")
        first = self.manager.active
        second = self.manager.new_session(self.workspace_b)
        self.assertTrue(self.manager.select_model(first.session_id, "one"))
        self.assertTrue(self.manager.select_model(second.session_id, "one"))
        for session in (first, second):
            self.factory.next_block = True
            self.assertTrue(session.controller.submit(f"turn-{session.session_id}"))
            session.messages.append(("你", f"turn-{session.session_id}"))
        self.assertFalse(self.manager.begin_close_all())
        for index, session in enumerate((first, second)):
            runtime = self.factory.runtimes[index]
            self.assertTrue(runtime.started.wait(1))
            runtime.release.set()
            event = next_result(session.controller)
            self.assertTrue(session.controller.consume_result())
            session.messages.append(("MyAgent", str(event.payload)))
            self.assertTrue(self.manager.persist_completed_turn(session.session_id))
            session.controller.close_agent_once()
        restored = SessionManager(
            store=self.store,
            default_cwd=self.workspace_a,
            runtime_factory=RuntimeFactory(),
        )
        self.addCleanup(restored.begin_close_all)
        self.assertEqual([len(item.messages) for item in restored.sessions], [2, 2])
        self.assertEqual(
            [len(item.controller.agent.snapshot_history()) for item in restored.sessions],
            [2, 2],
        )

    def test_delete_model_save_failure_rolls_back_store_and_loaded_runtime(self) -> None:
        self.manager.register_model("one", "key-one")
        session = self.manager.active
        self.assertTrue(self.manager.select_model(session.session_id, "one"))
        self.assertTrue(session.controller.submit("remember"))
        consume(session.controller)
        runtime = self.factory.runtimes[0]
        history = list(runtime.history)
        original_models = self.store.path.read_bytes()
        original_conversations = self.manager.conversation_store.path.read_bytes()
        with patch.object(
            self.manager.conversation_store,
            "save",
            side_effect=ConversationStoreError("disk unavailable"),
        ):
            self.assertFalse(self.manager.delete_model("one"))
        self.assertEqual(self.store.path.read_bytes(), original_models)
        self.assertEqual(
            self.manager.conversation_store.path.read_bytes(), original_conversations
        )
        self.assertEqual(session.controller.model, "one")
        self.assertEqual(runtime.history, history)
        self.assertTrue(session.controller.agent.loaded)
        self.assertEqual(runtime.reset_calls, 0)
        self.assertEqual(runtime.close_calls, 0)

    def test_missing_model_keeps_history_and_can_be_reselected(self) -> None:
        self.manager.register_model("one", "old-key")
        session = self.manager.active
        self.assertTrue(self.manager.select_model(session.session_id, "one"))
        history = [{"role": "user", "content": "remember me"}]
        session.controller.agent.restore_history(history)
        session.messages.append(("你", "remember me"))
        self.assertTrue(self.manager.persist_completed_turn(session.session_id))
        self.assertTrue(self.store.delete("one"))

        factory = RuntimeFactory()
        restored = SessionManager(
            store=self.store,
            default_cwd=self.workspace_a,
            runtime_factory=factory,
        )
        self.addCleanup(restored.begin_close_all)
        recovered = restored.active
        self.assertEqual(recovered.controller.model, "")
        self.assertEqual(recovered.controller.agent.snapshot_history(), history)
        self.assertIn("模型已缺失", restored.conversation_error)
        restored.register_model("one", "new-key")
        self.assertTrue(restored.select_model(recovered.session_id, "one"))
        self.assertTrue(recovered.controller.submit("continue"))
        consume(recovered.controller)
        self.assertEqual(factory.runtimes[0].history[:1], history)

    def test_all_missing_workspaces_fall_back_without_overwriting_source(self) -> None:
        missing = self.root / "missing"
        candidate = {
            "version": 1,
            "activeSessionId": 4,
            "nextSessionId": 5,
            "sessions": [{
                "id": 4,
                "title": "失效工作区",
                "workspace": str(missing.resolve()),
                "mainModel": "",
                "subModel": "",
                "kind": "main",
                "messages": [],
                "history": [],
            }],
        }
        self.manager.conversation_store.save(candidate)
        original = self.manager.conversation_store.path.read_text(encoding="utf-8")
        restored = SessionManager(
            store=self.store,
            default_cwd=self.workspace_a,
            runtime_factory=RuntimeFactory(),
        )
        self.addCleanup(restored.begin_close_all)
        self.assertEqual(restored.active.session_id, 5)
        self.assertEqual(restored.active.workspace, self.workspace_a.resolve())
        self.assertIn("安全空白对话", restored.conversation_error)
        self.assertEqual(
            self.manager.conversation_store.path.read_text(encoding="utf-8"), original
        )

    def test_delete_busy_rejected_then_clears_idle_selected_runtimes(self) -> None:
        self.manager.register_model("one", "key-one")
        first = self.manager.active
        second = self.manager.new_session(self.workspace_b)
        self.manager.select_model(first.session_id, "one")
        self.manager.select_model(second.session_id, "one")
        self.factory.next_block = True
        self.assertTrue(first.controller.submit("wait"))
        self.assertTrue(self.factory.runtimes[0].started.wait(1))
        self.assertFalse(self.manager.delete_model("one"))
        self.assertIn("运行中的对话", self.manager.last_error)
        self.factory.runtimes[0].release.set()
        consume(first.controller)

        self.assertTrue(self.manager.delete_model("one"))
        self.assertEqual(first.controller.model, "")
        self.assertEqual(second.controller.model, "")
        self.assertFalse(first.controller.submit("again"))
        self.assertEqual(self.manager.model_names, ())


class RuntimeCreationTests(unittest.TestCase):
    def test_main_and_sub_factories_receive_session_key_model_and_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            for kind, target in (
                ("main", "myagent.gui.create_default_agent"),
                ("sub", "myagent.gui.create_default_subagent"),
            ):
                with self.subTest(kind=kind), patch(
                    "myagent.gui._load_config", return_value={"base_url": "https://example.test/v1"}
                ), patch(
                    "myagent.gui._client_options", return_value={"max_retries": 0}
                ), patch(
                    "myagent.gui._config_from_environment",
                    return_value=SimpleNamespace(model="chosen"),
                ), patch(target, return_value=object()) as factory:
                    constructor = MagicMock(return_value=object())
                    with patch.dict(
                        sys.modules,
                        {"openai": SimpleNamespace(OpenAI=constructor)},
                    ):
                        result = create_runtime(
                            lambda request: False,
                            model="chosen",
                            api_key="session-secret",
                            base_url="https://model.example/v1",
                            cwd=folder,
                            kind=kind,
                        )
                    self.assertIsNotNone(result)
                    constructor.assert_called_once_with(
                        max_retries=0,
                        api_key="session-secret",
                        base_url="https://model.example/v1",
                    )
                    self.assertEqual(factory.call_args.kwargs["cwd"], Path(folder))


class TkSmokeTests(unittest.TestCase):
    def test_windows_app_identity_is_set_before_root_creation(self) -> None:
        setter = MagicMock(return_value=0)
        fake_ctypes = SimpleNamespace(
            c_long=object(),
            c_wchar_p=object(),
            windll=SimpleNamespace(
                shell32=SimpleNamespace(
                    SetCurrentProcessExplicitAppUserModelID=setter,
                )
            ),
        )
        with patch("myagent.gui.os.name", "nt"), patch(
            "myagent.gui.ctypes",
            fake_ctypes,
        ):
            self.assertTrue(_set_windows_app_id())
        setter.assert_called_once_with("Niooooo.MyAgent.Desktop")

        events: list[str] = []
        root = MagicMock()
        root.mainloop.side_effect = lambda: events.append("mainloop")
        with patch(
            "myagent.gui._set_windows_app_id",
            side_effect=lambda: events.append("app-id") or True,
        ), patch(
            "myagent.gui.ctk.CTk",
            side_effect=lambda **_kwargs: events.append("root") or root,
        ), patch(
            "myagent.gui.MyAgentWindow",
            side_effect=lambda _root: events.append("window"),
        ):
            main()
        self.assertEqual(events, ["app-id", "root", "window", "mainloop"])

    def test_withdrawn_window_constructs_real_widgets(self) -> None:
        try:
            root = ctk.CTk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        with tempfile.TemporaryDirectory() as folder:
            manager = SessionManager(
                store=ModelStore(Path(folder) / "models.json"),
                default_cwd=folder,
                runtime_factory=RuntimeFactory(),
            )
            window = MyAgentWindow(root, manager=manager)
            root.update_idletasks()
            self.assertEqual(window.file_menu.index("end"), 1)
            self.assertIn("新建对话", window.file_menu.entrycget(0, "label"))
            self.assertIn("打开文件夹", window.file_menu.entrycget(1, "label"))
            self.assertIsInstance(window.input, tk.Text)
            self.assertIsInstance(window.model_selector, ctk.CTkOptionMenu)
            self.assertEqual(window.transcript.cget("font")[1], 12)
            self.assertEqual(
                int(
                    root.tk.call(
                        "font",
                        "actual",
                        window.input.cget("font"),
                        "-size",
                    )
                ),
                12,
            )
            self.assertEqual(window.model_selector.cget("font")[1], 11)
            self.assertEqual(window.model_back_button.cget("font")[1], 10)
            self.assertEqual(window.send_button.cget("text"), "发送  ↑")
            self.assertEqual(window.send_button.cget("fg_color"), "#2d3947")
            self.assertEqual(window.send_button.cget("text_color"), "#f7f9ff")
            self.assertEqual(window.model_add_button.cget("fg_color"), "#275dce")
            self.assertEqual(window.welcome_panel.winfo_manager(), "grid")
            self.assertEqual(window.transcript.winfo_manager(), "")
            self.assertIsInstance(window.welcome_logo, tk.Canvas)
            self.assertEqual(
                window.welcome_panel.winfo_children(),
                [window.welcome_logo],
            )
            self.assertGreaterEqual(len(window.welcome_logo.find_all()), 8)
            self.assertFalse(window.welcome_logo.bind("<Configure>"))
            logo_items = window.welcome_logo.find_all()
            logo_snapshot = tuple(
                (
                    item,
                    window.welcome_logo.type(item),
                    tuple(window.welcome_logo.coords(item)),
                )
                for item in logo_items
            )
            send_grid_slot = (
                int(window.send_button.grid_info()["row"]),
                int(window.send_button.grid_info()["column"]),
            )
            model_add_grid_slot = (
                int(window.model_add_button.grid_info()["row"]),
                int(window.model_add_button.grid_info()["column"]),
            )
            window._on_input_focus(SimpleNamespace())
            self.assertNotEqual(window.composer.cget("border_color"), "#2a3439")
            window._on_input_blur(SimpleNamespace())
            self.assertEqual(window.composer.cget("border_color"), "#2a3439")
            self.assertEqual(
                window.kind_selector.cget("values"),
                [MAIN_AGENT_LABEL, SUBAGENT_LABEL],
            )
            self.assertEqual(window.conversation_page.winfo_manager(), "grid")
            self.assertEqual(window.models_page.winfo_manager(), "")
            self.assertEqual(window.model_back_button.cget("text"), "←  返回对话")
            window.show_models()
            self.assertEqual(window.conversation_page.winfo_manager(), "")
            self.assertEqual(window.models_page.winfo_manager(), "grid")
            window.model_back_button.invoke()
            self.assertEqual(window.conversation_page.winfo_manager(), "grid")
            self.assertEqual(window.models_page.winfo_manager(), "")
            window.show_models()
            self.assertEqual(window._on_escape(SimpleNamespace()), "break")
            self.assertEqual(window.conversation_page.winfo_manager(), "grid")
            self.assertIsNone(window._on_escape(SimpleNamespace()))
            tab_button = window._tab_widgets[1]
            self.assertIsInstance(tab_button, ctk.CTkButton)
            self.assertNotEqual(tab_button.cget("hover_color"), "#ffffff")
            self.assertEqual(window._app_icon.width(), 32)
            scale = ctk.ScalingTracker.get_window_scaling(root)
            manager.register_model("model-one", "key-one")
            manager.register_model("model-two", "key-two")
            window._refresh_model_choices()
            window._on_window_resize(
                SimpleNamespace(widget=root, width=round(720 * scale))
            )
            self.assertTrue(window._compact_layout)
            self.assertEqual(
                (
                    int(window.send_button.grid_info()["row"]),
                    int(window.send_button.grid_info()["column"]),
                ),
                send_grid_slot,
            )
            self.assertEqual(
                (
                    int(window.model_add_button.grid_info()["row"]),
                    int(window.model_add_button.grid_info()["column"]),
                ),
                model_add_grid_slot,
            )
            self.assertEqual(int(window.model_add_button.grid_info()["row"]), 0)
            self.assertEqual(window.welcome_logo.winfo_manager(), "grid")
            self.assertEqual(window.send_hint.winfo_manager(), "")
            window._on_window_resize(
                SimpleNamespace(widget=root, width=round(900 * scale))
            )
            self.assertTrue(window._compact_layout)
            window._on_window_resize(
                SimpleNamespace(widget=root, width=round(940 * scale))
            )
            self.assertFalse(window._compact_layout)
            self.assertEqual(window.send_hint.winfo_manager(), "grid")
            self.assertEqual(
                (
                    int(window.send_button.grid_info()["row"]),
                    int(window.send_button.grid_info()["column"]),
                ),
                send_grid_slot,
            )
            self.assertEqual(
                (
                    int(window.model_add_button.grid_info()["row"]),
                    int(window.model_add_button.grid_info()["column"]),
                ),
                model_add_grid_slot,
            )
            self.assertEqual(window.welcome_logo.find_all(), logo_items)
            self.assertEqual(
                tuple(
                    (
                        item,
                        window.welcome_logo.type(item),
                        tuple(window.welcome_logo.coords(item)),
                    )
                    for item in window.welcome_logo.find_all()
                ),
                logo_snapshot,
            )
            manager.active.messages.append(("你", "hello"))
            window._render_active()
            self.assertEqual(window.welcome_panel.winfo_manager(), "")
            self.assertEqual(window.transcript.winfo_manager(), "grid")
            self.assertIn("hello", window.transcript.get("1.0", "end-1c"))
            window.close()

    def test_success_message_and_approval_display_are_redacted_only_in_ui(self) -> None:
        try:
            root = ctk.CTk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        secret = "tk-success-secret"
        with tempfile.TemporaryDirectory() as folder:
            factory = RuntimeFactory()
            factory.next_result = f"response with {secret}"
            manager = SessionManager(
                store=ModelStore(Path(folder) / "models.json"),
                default_cwd=folder,
                runtime_factory=factory,
            )
            manager.register_model("model", secret)
            session = manager.active
            self.assertTrue(manager.select_model(session.session_id, "model"))
            window = MyAgentWindow(root, manager=manager)
            root.update_idletasks()

            self.assertTrue(session.controller.submit("hello"))
            event = next_result(session.controller)
            self.assertNotIn(secret, str(event.payload))
            self.assertIn("[redacted]", str(event.payload))
            session.controller.events.put(event)
            window._poll_events()
            transcript = window.transcript.get("1.0", "end-1c")
            self.assertNotIn(secret, repr(session.messages))
            self.assertNotIn(secret, transcript)
            self.assertIn("[redacted]", repr(session.messages))
            self.assertIn("[redacted]", transcript)

            original_arguments = {
                "command": "inspect",
                "nested": {"token": secret},
            }
            proposal = ApprovalProposal(
                "bash",
                original_arguments,
                f"reason contains {secret}",
            )
            with patch("myagent.gui.messagebox.askyesno", return_value=False) as ask:
                window._handle_approval(session, proposal)
            displayed = ask.call_args.args[1]
            self.assertNotIn(secret, displayed)
            self.assertIn("[redacted]", displayed)
            self.assertEqual(proposal.reason, f"reason contains {secret}")
            self.assertEqual(proposal.arguments, original_arguments)
            self.assertEqual(proposal.arguments["nested"]["token"], secret)
            window.close()


if __name__ == "__main__":
    unittest.main()
