import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from myagent.agent import AgentLoop, AgentLoopLimitError
from myagent.hooks import (
    HookExecutionError,
    HookRegistry,
    HookRejectedError,
    PostToolUse,
    PreToolUse,
    Stop,
    StopReason,
    UserPromptSubmit,
)
from myagent.tooling import FunctionTool, ToolRegistry
from myagent.tools import build_default_tool_registry
from tests.fakes import FakeResponses, function_call, response


class AgentLifecycleHookTests(unittest.TestCase):
    def setUp(self) -> None:
        # Hook protocol tests historically use short-lived Agents without closing
        # them. Scheduler shutdown is exercised by its dedicated lifecycle tests.
        scheduler_start = patch("myagent.composition.ScheduledTaskRuntime.start")
        scheduler_start.start()
        self.addCleanup(scheduler_start.stop)

    def test_user_prompt_can_be_validated_rewritten_and_given_context(self) -> None:
        hooks = HookRegistry()
        stops: list[Stop] = []

        def prepare_prompt(event: UserPromptSubmit) -> None:
            event.prompt = event.prompt.strip()
            event.add_context("workspace: demo")

        hooks.register(UserPromptSubmit, prepare_prompt)
        hooks.register(Stop, stops.append)
        responses = FakeResponses([response([], "done")])
        agent = AgentLoop(SimpleNamespace(responses=responses), hooks=hooks)

        answer = agent.run("  inspect files  ")

        self.assertEqual(answer, "done")
        self.assertEqual(
            responses.requests[0]["input"],
            [
                {"role": "developer", "content": "workspace: demo"},
                {"role": "user", "content": "inspect files"},
            ],
        )
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0].reason, StopReason.COMPLETED)
        self.assertEqual(stops[0].output_text, "done")
        self.assertIsNone(stops[0].error)

    def test_rejected_prompt_never_enters_loop_and_still_emits_stop(self) -> None:
        hooks = HookRegistry()
        stops: list[Stop] = []
        hooks.register(UserPromptSubmit, lambda event: event.reject("blocked input"))
        hooks.register(Stop, stops.append)
        responses = FakeResponses([])
        agent = AgentLoop(SimpleNamespace(responses=responses), hooks=hooks)

        with self.assertRaisesRegex(HookRejectedError, "blocked input"):
            agent.run("unsafe prompt")

        self.assertEqual(responses.requests, [])
        self.assertEqual(agent.history, [])
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0].reason, StopReason.ERROR)
        self.assertIsInstance(stops[0].error, HookRejectedError)

    def test_stop_receives_loop_limit_error_before_run_exits(self) -> None:
        hooks = HookRegistry()
        stops: list[Stop] = []
        hooks.register(Stop, stops.append)
        responses = FakeResponses([response([function_call()])])
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            max_tool_rounds=0,
            bash_tool=lambda command: {"ok": True},
            hooks=hooks,
        )

        with self.assertRaises(AgentLoopLimitError):
            agent.run("loop")

        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0].reason, StopReason.ERROR)
        self.assertEqual(stops[0].tool_rounds, 0)
        self.assertIsInstance(stops[0].error, AgentLoopLimitError)

    def test_stop_runs_for_invalid_input_and_failure_does_not_mask_error(self) -> None:
        hooks = HookRegistry()

        def failing_cleanup(event: Stop) -> None:
            raise RuntimeError("cleanup failed")

        hooks.register(Stop, failing_cleanup)
        agent = AgentLoop(SimpleNamespace(responses=FakeResponses([])), hooks=hooks)

        with self.assertRaises(TypeError) as raised:
            agent.run(None)  # type: ignore[arg-type]

        self.assertIn("user_input must be a string", str(raised.exception))
        self.assertTrue(
            any("cleanup failed" in note for note in raised.exception.__notes__)
        )

    def test_stop_failure_is_visible_after_an_otherwise_successful_run(self) -> None:
        hooks = HookRegistry()
        hooks.register(Stop, lambda event: 1 / 0)
        responses = FakeResponses([response([], "done")])
        agent = AgentLoop(SimpleNamespace(responses=responses), hooks=hooks)

        with self.assertRaisesRegex(HookExecutionError, "Stop hook"):
            agent.run("finish")


class ToolLifecycleHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hooks = HookRegistry()
        self.order: list[str] = []

        def handler(value: str) -> dict[str, object]:
            self.order.append("handler")
            return {"ok": True, "value": value}

        self.registry = ToolRegistry(
            [
                FunctionTool(
                    name="echo",
                    description="Echo a value",
                    parameters={
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                    handler=handler,
                )
            ],
            hooks=self.hooks,
        )

    def test_pre_and_post_wrap_handler_in_order_and_keep_call_id(self) -> None:
        seen_call_ids = []

        def before(event: PreToolUse) -> None:
            self.order.append("pre")
            seen_call_ids.append(event.call_id)
            self.assertEqual(dict(event.arguments), {"value": "hello"})

        def after(event: PostToolUse) -> None:
            self.order.append("post")
            seen_call_ids.append(event.call_id)
            event.replace_result({"ok": True, "value": "HELLO"})

        self.hooks.register(PreToolUse, before)
        self.hooks.register(PostToolUse, after)

        result = self.registry.execute(
            "echo",
            json.dumps({"value": "hello"}),
            call_id="call_echo",
        )

        self.assertEqual(self.order, ["pre", "handler", "post"])
        self.assertEqual(seen_call_ids, ["call_echo", "call_echo"])
        self.assertEqual(result, {"ok": True, "value": "HELLO"})

    def test_pre_can_deny_execution_while_later_audit_hook_still_runs(self) -> None:
        def deny(event: PreToolUse) -> None:
            self.order.append("policy")
            event.deny("not allowed")

        def audit(event: PreToolUse) -> None:
            self.order.append(f"audit:{event.denial_reason}")

        self.hooks.register(PreToolUse, deny)
        self.hooks.register(PreToolUse, audit)

        result = self.registry.execute("echo", '{"value":"hello"}')

        self.assertFalse(result["ok"])
        self.assertEqual(result["hook"], "PreToolUse")
        self.assertEqual(self.order, ["policy", "audit:not allowed"])

    def test_post_runs_for_handler_errors_and_can_validate_output(self) -> None:
        def failing_handler() -> dict[str, object]:
            raise RuntimeError("boom")

        registry = ToolRegistry(
            [
                FunctionTool(
                    name="failing",
                    description="Fail",
                    parameters={"type": "object", "properties": {}},
                    handler=failing_handler,
                )
            ],
            hooks=self.hooks,
        )

        def validate(event: PostToolUse) -> None:
            self.assertFalse(event.result["ok"])
            event.result["checked"] = True

        self.hooks.register(PostToolUse, validate)

        result = registry.execute("failing", "{}")

        self.assertFalse(result["ok"])
        self.assertTrue(result["checked"])
        self.assertIn("boom", result["error"])

    def test_failing_pre_hook_is_returned_in_band_without_execution(self) -> None:
        self.hooks.register(PreToolUse, lambda event: 1 / 0)

        result = self.registry.execute("echo", '{"value":"hello"}')

        self.assertFalse(result["ok"])
        self.assertEqual(result["hook"], "PreToolUse")
        self.assertIn("failed", result["error"])
        self.assertEqual(self.order, [])

    def test_default_permissions_are_applied_as_first_pre_hook(self) -> None:
        hooks = HookRegistry()
        seen_denials = []
        executed = []
        hooks.register(
            PreToolUse,
            lambda event: seen_denials.append(event.denial_reason),
        )
        registry = build_default_tool_registry(
            bash_tool=lambda command: executed.append(command) or {"ok": True},
            hooks=hooks,
        )

        result = registry.execute("bash", '{"command":"rm note.txt"}')

        self.assertEqual(result["permission"], "denied")
        self.assertEqual(executed, [])
        self.assertEqual(len(seen_denials), 1)
        self.assertIn("no user approval handler", seen_denials[0])


if __name__ == "__main__":
    unittest.main()
