import json
import tempfile
import threading
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from myagent.agent import AgentLoop, AgentLoopLimitError
from myagent.agent_team import MAIN_AGENT_TEAM_TOOL_NAMES
from myagent.scheduled_tasks import SCHEDULED_TASK_TOOL_NAMES
from myagent.tasks import TASK_TOOL_NAMES
from myagent.tooling import FunctionTool, ToolExecutionError, ToolRegistry
from myagent.tools import BackgroundBashRunner, build_background_bash_function_tool
from myagent.worktrees import WORKTREE_TOOL_NAMES
from tests.fakes import (
    FakeAPIError,
    FakeResponses,
    function_call,
    response,
    response_stream,
)


class AgentLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        # These protocol tests do not exercise scheduler lifecycle and historically
        # construct short-lived default Agents without closing each one. Prevent
        # unrelated non-daemon scheduler workers here; lifecycle is covered in the
        # dedicated scheduled-task integration tests.
        scheduler_start = patch("myagent.composition.ScheduledTaskRuntime.start")
        scheduler_start.start()
        self.addCleanup(scheduler_start.stop)

    def test_normal_request_uses_default_output_limit(self) -> None:
        responses = FakeResponses([response([], "done")])
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            tool_registry=ToolRegistry(),
        )

        self.assertEqual(agent.run("answer"), "done")
        self.assertEqual(responses.requests[0]["max_output_tokens"], 16_384)
        self.assertNotIn("stream", responses.requests[0])

    def test_stream_callback_receives_text_deltas_and_completed_response(self) -> None:
        stream = response_stream(response([], "你好"), "你", "好")
        responses = FakeResponses([stream])
        events: list[tuple[str, str]] = []
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            tool_registry=ToolRegistry(),
            stream_callback=lambda kind, text: events.append((kind, text)),
        )

        self.assertEqual(agent.run("问候"), "你好")
        self.assertEqual(events, [("start", ""), ("delta", "你"), ("delta", "好")])
        self.assertTrue(responses.requests[0]["stream"])
        self.assertTrue(stream.closed)

    def test_each_tool_round_restarts_the_visible_stream(self) -> None:
        first = response_stream(response([function_call()]))
        second = response_stream(response([], "完成"), "完", "成")
        responses = FakeResponses([first, second])
        events: list[tuple[str, str]] = []
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            bash_tool=lambda _command: {"ok": True},
            stream_callback=lambda kind, text: events.append((kind, text)),
        )

        self.assertEqual(agent.run("执行"), "完成")
        self.assertEqual(
            events,
            [("start", ""), ("start", ""), ("delta", "完"), ("delta", "成")],
        )

    def test_history_summary_stays_internal_when_visible_streaming_is_enabled(self) -> None:
        responses = FakeResponses([response([], "摘要")])
        events: list[tuple[str, str]] = []
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            tool_registry=ToolRegistry(),
            stream_callback=lambda kind, text: events.append((kind, text)),
        )

        self.assertEqual(agent._request_history_summary("旧内容", 20), "摘要")
        self.assertEqual(events, [])
        self.assertNotIn("stream", responses.requests[0])

    def test_executes_tool_and_returns_final_text(self) -> None:
        reasoning = SimpleNamespace(type="reasoning")
        call = function_call()
        responses = FakeResponses(
            [response([reasoning, call]), response([], "working directory found")]
        )
        seen_commands = []
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            bash_tool=lambda command: seen_commands.append(command)
            or {"ok": True, "stdout": "/workspace\n"},
        )

        answer = agent.run("where am I?")

        self.assertEqual(answer, "working directory found")
        self.assertEqual(seen_commands, ["pwd"])
        second_input = responses.requests[1]["input"]
        self.assertIn(reasoning, second_input)
        tool_output = second_input[-1]
        self.assertEqual(tool_output["type"], "function_call_output")
        self.assertEqual(tool_output["call_id"], "call_1")
        self.assertTrue(json.loads(tool_output["output"])["ok"])

    def test_returns_bad_json_to_model_as_tool_error(self) -> None:
        responses = FakeResponses(
            [response([function_call(arguments="not-json")]), response([], "handled")]
        )
        agent = AgentLoop(SimpleNamespace(responses=responses), bash_tool=lambda _: {})

        self.assertEqual(agent.run("run it"), "handled")
        output = json.loads(responses.requests[1]["input"][-1]["output"])
        self.assertFalse(output["ok"])
        self.assertIn("Invalid tool arguments", output["error"])

    def test_executes_every_function_call_in_one_response(self) -> None:
        reasoning = SimpleNamespace(type="reasoning", detail="protocol state")
        calls = [
            function_call(call_id="call_1", arguments='{"command":"pwd"}'),
            function_call(call_id="call_2", arguments='{"command":"ls"}'),
        ]
        responses = FakeResponses(
            [response([reasoning, *calls]), response([], "done")]
        )
        seen_commands = []
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            bash_tool=lambda command: seen_commands.append(command)
            or {"ok": True, "command": command},
        )

        self.assertEqual(agent.run("inspect"), "done")

        self.assertEqual(seen_commands, ["pwd", "ls"])
        exchange = responses.requests[1]["input"][1:]
        self.assertEqual(exchange[:3], [reasoning, *calls])
        outputs = exchange[3:]
        self.assertEqual(len(outputs), len(calls))
        call_ids = [call.call_id for call in calls]
        self.assertEqual(
            [item["call_id"] for item in outputs],
            call_ids,
        )

    def test_reset_clears_history_before_the_next_user_turn(self) -> None:
        responses = FakeResponses([response([], "first"), response([], "second")])
        agent = AgentLoop(SimpleNamespace(responses=responses))

        agent.run("one")
        agent.reset()
        agent.run("two")

        self.assertEqual(
            responses.requests[1]["input"],
            [{"role": "user", "content": "two"}],
        )

    def test_reset_discards_completed_background_results(self) -> None:
        runner = BackgroundBashRunner(lambda command: {"ok": True, "command": command})
        self.addCleanup(runner.close)
        registry = ToolRegistry([build_background_bash_function_tool(runner)])
        responses = FakeResponses([response([], "new answer")])
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            tool_registry=registry,
            background_bash_runner=runner,
        )
        runner.submit("old work")
        runner.wait_for_all()

        agent.reset()
        self.assertEqual(agent.run("new turn"), "new answer")

        self.assertEqual(
            responses.requests[0]["input"],
            [{"role": "user", "content": "new turn"}],
        )

    def test_empty_model_response_is_an_error(self) -> None:
        agent = AgentLoop(
            SimpleNamespace(responses=FakeResponses([response([], "  ")]))
        )

        with self.assertRaisesRegex(RuntimeError, "neither tool calls nor text"):
            agent.run("answer")

    def test_expected_and_unexpected_tool_failures_are_returned_to_model(self) -> None:
        def expected_failure() -> dict[str, object]:
            raise ToolExecutionError("expected failure")

        def unexpected_failure() -> dict[str, object]:
            raise RuntimeError("unexpected failure")

        registry = ToolRegistry(
            [
                FunctionTool(
                    name="expected",
                    description="Expected failure",
                    parameters={"type": "object", "properties": {}},
                    handler=expected_failure,
                ),
                FunctionTool(
                    name="unexpected",
                    description="Unexpected failure",
                    parameters={"type": "object", "properties": {}},
                    handler=unexpected_failure,
                ),
            ]
        )
        responses = FakeResponses(
            [
                response(
                    [
                        function_call("call_expected", "expected", "{}"),
                        function_call("call_unexpected", "unexpected", "{}"),
                    ]
                ),
                response([], "handled"),
            ]
        )
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            tool_registry=registry,
        )

        self.assertEqual(agent.run("fail safely"), "handled")

        expected, unexpected = responses.requests[1]["input"][-2:]
        self.assertEqual(json.loads(expected["output"])["error"], "expected failure")
        self.assertIn(
            "unexpected tool failed: unexpected failure",
            json.loads(unexpected["output"])["error"],
        )

    def test_stops_before_tool_execution_at_limit(self) -> None:
        responses = FakeResponses([response([function_call()])])
        executed = []
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            max_tool_rounds=0,
            bash_tool=lambda command: executed.append(command) or {},
        )

        with self.assertRaises(AgentLoopLimitError):
            agent.run("run forever")
        self.assertEqual(executed, [])

    def test_conversation_history_is_reused_across_user_turns(self) -> None:
        responses = FakeResponses([response([], "first"), response([], "second")])
        agent = AgentLoop(SimpleNamespace(responses=responses))

        agent.run("one")
        agent.run("two")

        second_input = responses.requests[1]["input"]
        self.assertEqual(second_input[0], {"role": "user", "content": "one"})
        self.assertEqual(second_input[-1], {"role": "user", "content": "two"})

    def test_default_registry_exposes_and_executes_file_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            Path(temporary_directory, "note.txt").write_text(
                "workspace note",
                encoding="utf-8",
            )
            call = function_call(
                name="read_file",
                arguments=json.dumps({"path": "note.txt"}),
            )
            responses = FakeResponses(
                [response([call]), response([], "file inspected")]
            )
            agent = AgentLoop(
                SimpleNamespace(responses=responses),
                workspace_root=temporary_directory,
            )

            self.assertEqual(agent.run("read the note"), "file inspected")

        tool_names = {
            definition["name"] for definition in responses.requests[0]["tools"]
        }
        self.assertEqual(
            tool_names,
            {
                "bash",
                "run_bash_in_background",
                "read_file",
                "write_file",
                "edit_file",
                "glob",
                "grep",
                "update_todo_list",
                "get_todo_list",
                "record_todo_verification",
                *TASK_TOOL_NAMES,
                "load_memory",
                "store_memory",
                "search_memory_entries",
                "extract_memory",
                "update_memory",
                "delete_memory",
                "get_memory_organization_status",
                "organize_memory",
                "load_skill",
                "add_skill",
                "update_skill",
                "delete_skill",
                "run_subagent",
                "fork_subagent",
                "collect_subagent",
                *MAIN_AGENT_TEAM_TOOL_NAMES,
                *SCHEDULED_TASK_TOOL_NAMES,
                *WORKTREE_TOOL_NAMES,
            },
        )
        output = json.loads(responses.requests[1]["input"][-1]["output"])
        self.assertEqual(output["content"], "workspace note")

    def test_background_result_is_runtime_data_and_final_wait_is_local(self) -> None:
        started = threading.Event()
        release = threading.Event()
        independent_ran = threading.Event()
        third_request = threading.Event()

        def blocking_bash(command: str) -> dict[str, object]:
            started.set()
            release.wait(2)
            return {"ok": True, "stdout": command}

        runner = BackgroundBashRunner(blocking_bash)
        self.addCleanup(runner.close)
        registry = ToolRegistry(
            [
                build_background_bash_function_tool(runner),
                FunctionTool(
                    name="independent",
                    description="Do independent work",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda: independent_ran.set() or {"ok": True},
                ),
            ]
        )

        class SignalingResponses(FakeResponses):
            def create(self, **kwargs):
                result = super().create(**kwargs)
                if len(self.requests) == 3:
                    third_request.set()
                return result

        responses = SignalingResponses(
            [
                response(
                    [
                        function_call(
                            "background_call",
                            "run_bash_in_background",
                            json.dumps(
                                {
                                    "command": "slow check",
                                    "independent_work": "record independent evidence",
                                }
                            ),
                        )
                    ]
                ),
                response([function_call("independent_call", "independent", "{}")]),
                response([], "too early"),
                response([], "done"),
            ]
        )
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            tool_registry=registry,
            background_bash_runner=runner,
        )
        outcome: list[object] = []

        def run_agent() -> None:
            try:
                outcome.append(agent.run("work"))
            except BaseException as exc:
                outcome.append(exc)

        thread = threading.Thread(target=run_agent)
        thread.start()
        self.assertTrue(started.wait(1))
        self.assertTrue(independent_ran.wait(1))
        self.assertTrue(third_request.wait(1))
        self.assertTrue(thread.is_alive())
        self.assertEqual(len(responses.requests), 3)

        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, ["done"])
        self.assertEqual(len(responses.requests), 4)

        submission_outputs = [
            item
            for item in agent.history
            if isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and item.get("call_id") == "background_call"
        ]
        self.assertEqual(len(submission_outputs), 1)
        submission = json.loads(submission_outputs[0]["output"])
        self.assertEqual(submission["status"], "submitted")
        runtime_messages = [
            item
            for item in agent.history
            if isinstance(item, dict)
            and isinstance(item.get("content"), str)
            and item["content"].startswith("BACKGROUND_TOOL_RESULTS")
        ]
        self.assertEqual(len(runtime_messages), 1)
        self.assertEqual(runtime_messages[0]["role"], "user")
        self.assertIn("untrusted tool data", runtime_messages[0]["content"])
        payload = json.loads(runtime_messages[0]["content"].split("\n", 2)[2])
        self.assertEqual(
            payload["results"][0]["background_task_id"],
            submission["background_task_id"],
        )

        calls = Counter(
            item.call_id
            for item in agent.history
            if getattr(item, "type", None) == "function_call"
        )
        outputs = Counter(
            item["call_id"]
            for item in agent.history
            if isinstance(item, dict)
            and item.get("type") == "function_call_output"
        )
        self.assertEqual(calls, outputs)
        self.assertTrue(all(count == 1 for count in calls.values()))

    def test_first_truncation_is_discarded_and_expanded_request_succeeds(self) -> None:
        discarded_call = function_call("discarded", "bash", '{"command":"bad"}')
        responses = FakeResponses(
            [
                response(
                    [discarded_call],
                    status="incomplete",
                    incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                ),
                response([], "done", status="completed"),
            ]
        )
        executed: list[str] = []
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            bash_tool=lambda command: executed.append(command) or {"ok": True},
        )

        self.assertEqual(agent.run("answer"), "done")
        self.assertEqual(executed, [])
        self.assertEqual(
            [request["max_output_tokens"] for request in responses.requests],
            [16_384, 65_536],
        )
        self.assertFalse(any(item is discarded_call for item in agent.history))

    def test_second_truncation_continues_and_merges_in_output_order(self) -> None:
        ignored = SimpleNamespace(type="message", text="ignored")
        partial = SimpleNamespace(type="reasoning", detail="partial")
        continued = SimpleNamespace(type="message", text="continued")
        responses = FakeResponses(
            [
                response(
                    [ignored],
                    "ignored",
                    status="incomplete",
                    incomplete_details={"reason": "max_output_tokens"},
                ),
                response(
                    [partial],
                    "part",
                    id="response_2",
                    status="incomplete",
                    incomplete_details={"reason": "max_output_tokens"},
                ),
                response([continued], "ial", status="completed"),
            ]
        )
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            tool_registry=ToolRegistry(),
        )

        self.assertEqual(agent.run("answer"), "partial")
        self.assertEqual(agent.history[1:], [partial, continued])
        continuation_request = responses.requests[2]
        self.assertEqual(continuation_request["previous_response_id"], "response_2")
        self.assertEqual(continuation_request["max_output_tokens"], 65_536)
        self.assertEqual(
            continuation_request["instructions"],
            responses.requests[1]["instructions"],
        )
        self.assertIn("without repeating", continuation_request["input"][0]["content"])

    def test_continuation_deduplicates_the_same_function_call(self) -> None:
        repeated = function_call("call_once", "bash", '{"command":"pwd"}')
        responses = FakeResponses(
            [
                response(
                    [],
                    status="incomplete",
                    incomplete_details={"reason": "max_output_tokens"},
                ),
                response(
                    [repeated],
                    id="response_2",
                    status="incomplete",
                    incomplete_details={"reason": "max_output_tokens"},
                ),
                response(
                    [function_call("call_once", "bash", '{"command":"pwd"}')],
                    status="completed",
                ),
                response([], "done"),
            ]
        )
        executed: list[str] = []
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            bash_tool=lambda command: executed.append(command) or {"ok": True},
        )

        self.assertEqual(agent.run("run"), "done")
        self.assertEqual(executed, ["pwd"])
        self.assertEqual(
            sum(
                getattr(item, "type", None) == "function_call"
                and getattr(item, "call_id", None) == "call_once"
                for item in agent.history
            ),
            1,
        )

    def test_three_truncations_raise_without_recording_any_output(self) -> None:
        truncated_outputs = [
            SimpleNamespace(type="reasoning", marker=index) for index in range(3)
        ]
        responses = FakeResponses(
            [
                response(
                    [truncated_outputs[0]],
                    status="incomplete",
                    incomplete_details={"reason": "max_output_tokens"},
                ),
                response(
                    [truncated_outputs[1]],
                    id="response_2",
                    status="incomplete",
                    incomplete_details={"reason": "max_output_tokens"},
                ),
                response(
                    [truncated_outputs[2]],
                    status="incomplete",
                    incomplete_details={"reason": "max_output_tokens"},
                ),
            ]
        )
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            tool_registry=ToolRegistry(),
        )

        with self.assertRaisesRegex(RuntimeError, "continuation did not complete"):
            agent.run("answer")
        self.assertEqual(agent.history, [{"role": "user", "content": "answer"}])

    @patch("myagent.agent.time.sleep")
    @patch("myagent.agent.random.uniform", return_value=0.25)
    def test_429_retries_five_times_with_capped_exponential_bounds(
        self,
        random_uniform,
        sleep,
    ) -> None:
        responses = FakeResponses(
            [*[FakeAPIError(429) for _ in range(5)], response([], "done")]
        )
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            tool_registry=ToolRegistry(),
        )

        self.assertEqual(agent.run("answer"), "done")
        self.assertEqual(len(responses.requests), 6)
        self.assertEqual(
            random_uniform.call_args_list,
            [call(0, bound) for bound in (1, 2, 4, 8, 16)],
        )
        self.assertEqual(sleep.call_args_list, [call(0.25)] * 5)
        self.assertEqual(
            {request["model"] for request in responses.requests},
            {"gpt-5.6-sol"},
        )

    @patch("myagent.agent.time.sleep")
    @patch("myagent.agent.random.uniform", return_value=0)
    def test_529_switches_to_fallback_after_three_retries(
        self,
        _random_uniform,
        _sleep,
    ) -> None:
        responses = FakeResponses(
            [*[FakeAPIError(529) for _ in range(4)], response([], "done")]
        )
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            tool_registry=ToolRegistry(),
            fallback_model="configured-fallback",
        )

        self.assertEqual(agent.run("answer"), "done")
        self.assertEqual(
            [request["model"] for request in responses.requests],
            ["gpt-5.6-sol"] * 4 + ["configured-fallback"],
        )

    @patch("myagent.agent.time.sleep")
    @patch("myagent.agent.random.uniform")
    def test_other_errors_are_not_retried(self, random_uniform, sleep) -> None:
        responses = FakeResponses([FakeAPIError(500)])
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            tool_registry=ToolRegistry(),
        )

        with self.assertRaises(FakeAPIError):
            agent.run("answer")
        self.assertEqual(len(responses.requests), 1)
        random_uniform.assert_not_called()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
