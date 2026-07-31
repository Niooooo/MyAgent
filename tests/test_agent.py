import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from myagent.agent import AgentLoop, AgentLoopLimitError
from myagent.tooling import FunctionTool, ToolExecutionError, ToolRegistry
from tests.fakes import FakeResponses, function_call, response


class AgentLoopTests(unittest.TestCase):
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
        calls = [
            function_call(call_id="call_1", arguments='{"command":"pwd"}'),
            function_call(call_id="call_2", arguments='{"command":"ls"}'),
        ]
        responses = FakeResponses([response(calls), response([], "done")])
        seen_commands = []
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            bash_tool=lambda command: seen_commands.append(command)
            or {"ok": True, "command": command},
        )

        self.assertEqual(agent.run("inspect"), "done")

        self.assertEqual(seen_commands, ["pwd", "ls"])
        outputs = responses.requests[1]["input"][-2:]
        self.assertEqual(
            [item["call_id"] for item in outputs],
            ["call_1", "call_2"],
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
                "read_file",
                "write_file",
                "edit_file",
                "glob",
                "grep",
                "update_todo_list",
                "get_todo_list",
                "record_todo_verification",
            },
        )
        output = json.loads(responses.requests[1]["input"][-1]["output"])
        self.assertEqual(output["content"], "workspace note")


if __name__ == "__main__":
    unittest.main()
