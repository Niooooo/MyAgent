import json
import threading
import unittest
from types import SimpleNamespace
from typing import Callable

from myagent.agent import AgentLoop
from myagent.hooks import (
    HookRegistry,
    PostToolUse,
    PreToolUse,
    Stop,
    UserPromptSubmit,
)
from myagent.subagents import SUBAGENT_TOOL_NAMES, SubAgentManager
from tests.fakes import FakeResponses, function_call, response


class SubAgentIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agents: list[AgentLoop] = []

    def tearDown(self) -> None:
        for agent in self.agents:
            agent.close()

    def make_agent(
        self,
        responses: FakeResponses,
        **kwargs: object,
    ) -> AgentLoop:
        agent = AgentLoop(SimpleNamespace(responses=responses), **kwargs)
        self.agents.append(agent)
        return agent

    def test_default_tools_are_visible_and_custom_allowlist_hides_and_denies(self) -> None:
        default_agent = self.make_agent(FakeResponses([]))
        visible = {
            definition["name"]
            for definition in default_agent.tool_registry.definitions
        }

        self.assertTrue(SUBAGENT_TOOL_NAMES.issubset(visible))

        restricted_agent = self.make_agent(
            FakeResponses([]),
            allowed_tools={"read_file"},
        )
        restricted_visible = {
            definition["name"]
            for definition in restricted_agent.tool_registry.definitions
        }
        forged = restricted_agent.tool_registry.execute(
            "run_subagent",
            '{"task":"try to bypass the allowlist"}',
        )

        self.assertEqual(restricted_visible, {"read_file"})
        self.assertFalse(forged["ok"])
        self.assertEqual(forged["permission"], "denied")
        self.assertIn("allowlist", forged["error"])

    def test_sync_result_contains_only_final_text_and_no_child_protocol_state(self) -> None:
        child_reasoning = SimpleNamespace(type="reasoning", detail="private")
        child_call = function_call(
            call_id="child_call",
            name="bash",
            arguments='{"command":"pwd"}',
        )
        responses = FakeResponses(
            [
                response(
                    [
                        function_call(
                            call_id="parent_call",
                            name="run_subagent",
                            arguments='{"task":"inspect the workspace"}',
                        )
                    ]
                ),
                response([child_reasoning, child_call]),
                response([], "child final text"),
                response([], "parent final text"),
            ]
        )
        commands: list[str] = []
        agent = self.make_agent(
            responses,
            bash_tool=lambda command: commands.append(command)
            or {"ok": True, "stdout": "workspace"},
        )

        answer = agent.run("delegate this task")

        self.assertEqual(answer, "parent final text")
        self.assertEqual(commands, ["pwd"])
        self.assertNotIn(
            "run_bash_in_background",
            {definition["name"] for definition in responses.requests[1]["tools"]},
        )
        self.assertNotIn(child_reasoning, agent.history)
        self.assertNotIn(child_call, agent.history)
        self.assertFalse(
            any(
                isinstance(item, dict) and item.get("call_id") == "child_call"
                for item in agent.history
            )
        )
        parent_output = json.loads(agent.history[-1]["output"])
        self.assertEqual(parent_output, {"ok": True, "output": "child final text"})

    def test_two_sync_calls_create_isolated_child_histories(self) -> None:
        responses = FakeResponses(
            [response([], "first result"), response([], "second result")]
        )
        agent = self.make_agent(responses)

        first = agent.tool_registry.execute(
            "run_subagent",
            '{"task":"first task"}',
        )
        second = agent.tool_registry.execute(
            "run_subagent",
            '{"task":"second task"}',
        )

        self.assertEqual(first, {"ok": True, "output": "first result"})
        self.assertEqual(second, {"ok": True, "output": "second result"})
        self.assertEqual(
            responses.requests[0]["input"],
            [{"role": "user", "content": "first task"}],
        )
        self.assertEqual(
            responses.requests[1]["input"],
            [{"role": "user", "content": "second task"}],
        )
        self.assertIsNot(
            responses.requests[0]["input"],
            responses.requests[1]["input"],
        )
        self.assertEqual(agent.history, [])

    def test_child_definitions_and_execution_both_exclude_management_tools(self) -> None:
        nested_call = function_call(
            call_id="nested",
            name="fork_subagent",
            arguments='{"task":"forbidden recursion"}',
        )
        responses = FakeResponses(
            [response([nested_call]), response([], "recursion refused")]
        )
        agent = self.make_agent(responses)

        result = agent.tool_registry.execute(
            "run_subagent",
            '{"task":"attempt nested delegation"}',
        )

        child_names = {
            definition["name"] for definition in responses.requests[0]["tools"]
        }
        nested_output = json.loads(responses.requests[1]["input"][-1]["output"])
        self.assertTrue(SUBAGENT_TOOL_NAMES.isdisjoint(child_names))
        self.assertEqual(nested_output["error"], "Unknown tool: fork_subagent")
        self.assertEqual(result, {"ok": True, "output": "recursion refused"})

    def test_child_tools_keep_hooks_permission_and_original_call_id(self) -> None:
        hooks = HookRegistry()
        prompts: list[str] = []
        stops: list[Stop] = []
        pre_events: list[tuple[str, str | None, str | None]] = []
        post_events: list[tuple[str, str | None]] = []
        hooks.register(UserPromptSubmit, lambda event: prompts.append(event.prompt))
        hooks.register(
            PreToolUse,
            lambda event: pre_events.append(
                (event.tool_name, event.call_id, event.denial_reason)
            ),
        )
        hooks.register(
            PostToolUse,
            lambda event: post_events.append((event.tool_name, event.call_id)),
        )
        hooks.register(Stop, stops.append)
        responses = FakeResponses(
            [
                response(
                    [
                        function_call(
                            call_id="child_bash_call",
                            name="bash",
                            arguments='{"command":"pwd"}',
                        )
                    ]
                ),
                response([], "done"),
            ]
        )
        commands: list[str] = []
        agent = self.make_agent(
            responses,
            hooks=hooks,
            bash_tool=lambda command: commands.append(command) or {"ok": True},
        )

        result = agent.tool_registry.execute(
            "run_subagent",
            '{"task":"use an ordinary tool"}',
            call_id="parent_run_call",
        )

        self.assertEqual(result, {"ok": True, "output": "done"})
        self.assertEqual(commands, ["pwd"])
        self.assertEqual(prompts, ["use an ordinary tool"])
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0].output_text, "done")
        self.assertIn(("bash", "child_bash_call", None), pre_events)
        self.assertIn(("bash", "child_bash_call"), post_events)
        self.assertEqual(
            responses.requests[1]["input"][-1]["call_id"],
            "child_bash_call",
        )

    def test_child_sensitive_calls_are_denied_or_approved_once(self) -> None:
        denied_hooks = HookRegistry()
        seen_denials: list[str | None] = []
        denied_hooks.register(
            PreToolUse,
            lambda event: seen_denials.append(event.denial_reason)
            if event.tool_name == "bash"
            else None,
        )
        denied_responses = FakeResponses(
            [
                response(
                    [
                        function_call(
                            "denied_call",
                            "bash",
                            '{"command":"rm note.txt"}',
                        )
                    ]
                ),
                response([], "denial handled"),
            ]
        )
        denied_commands: list[str] = []
        denied_agent = self.make_agent(
            denied_responses,
            hooks=denied_hooks,
            bash_tool=lambda command: denied_commands.append(command)
            or {"ok": True},
        )

        denied_agent.tool_registry.execute(
            "run_subagent",
            '{"task":"try a sensitive command"}',
        )

        self.assertEqual(denied_commands, [])
        self.assertEqual(len(seen_denials), 1)
        self.assertIn("no user approval handler", seen_denials[0])

        approvals: list[str] = []
        approval_answers = iter([True, False])
        approved_responses = FakeResponses(
            [
                response(
                    [
                        function_call(
                            "approved_once",
                            "bash",
                            '{"command":"rm first.txt"}',
                        ),
                        function_call(
                            "denied_next",
                            "bash",
                            '{"command":"rm second.txt"}',
                        ),
                    ]
                ),
                response([], "approval handled"),
            ]
        )
        approved_commands: list[str] = []
        approved_agent = self.make_agent(
            approved_responses,
            approval_callback=lambda request: approvals.append(
                request.arguments["command"]
            )
            or next(approval_answers),
            bash_tool=lambda command: approved_commands.append(command)
            or {"ok": True},
        )

        approved_agent.tool_registry.execute(
            "run_subagent",
            '{"task":"make two sensitive calls"}',
        )

        self.assertEqual(approvals, ["rm first.txt", "rm second.txt"])
        self.assertEqual(approved_commands, ["rm first.txt"])
        first_output, second_output = approved_responses.requests[1]["input"][-2:]
        self.assertTrue(json.loads(first_output["output"])["ok"])
        self.assertEqual(
            json.loads(second_output["output"])["permission"],
            "denied",
        )

    def test_repeated_child_creation_does_not_accumulate_main_hooks(self) -> None:
        hooks = HookRegistry()
        hooks.register(PreToolUse, lambda event: None)
        hooks.register(PostToolUse, lambda event: None)
        responses = FakeResponses([response([], "one"), response([], "two")])
        agent = self.make_agent(responses, hooks=hooks)
        before = {
            event_type: len(hooks.handlers_for(event_type))
            for event_type in (UserPromptSubmit, PreToolUse, PostToolUse, Stop)
        }

        agent.tool_registry.execute("run_subagent", '{"task":"one"}')
        agent.tool_registry.execute("run_subagent", '{"task":"two"}')

        after = {
            event_type: len(hooks.handlers_for(event_type))
            for event_type in (UserPromptSubmit, PreToolUse, PostToolUse, Stop)
        }
        self.assertEqual(after, before)


class _StubAgent:
    def __init__(
        self,
        runner: Callable[[str], str],
        closed: list["_StubAgent"],
    ) -> None:
        self._runner = runner
        self._closed_agents = closed
        self.history: list[str] = []

    def run(self, task: str) -> str:
        self.history.append(task)
        return self._runner(task)

    def close(self) -> None:
        self._closed_agents.append(self)


class SubAgentManagerTests(unittest.TestCase):
    def test_fork_returns_while_running_then_times_out_completes_and_cleans(self) -> None:
        started = threading.Event()
        release = threading.Event()
        closed: list[_StubAgent] = []

        def run(task: str) -> str:
            started.set()
            if not release.wait(2):
                raise RuntimeError("test release was not signaled")
            return f"finished: {task}"

        manager = SubAgentManager(
            lambda: _StubAgent(run, closed),
            max_workers=1,
            max_tasks=2,
        )
        self.addCleanup(manager.close)

        forked = manager.fork_subagent("background work")

        self.assertTrue(forked["ok"])
        self.assertEqual(forked["status"], "running")
        self.assertTrue(started.wait(1))
        fork_id = forked["fork_id"]
        self.assertEqual(
            manager.collect_subagent(fork_id),
            {"ok": True, "fork_id": fork_id, "status": "running"},
        )
        timed_out = manager.collect_subagent(
            fork_id,
            wait=True,
            timeout_seconds=0,
        )
        self.assertEqual(timed_out["code"], "timeout")
        self.assertEqual(timed_out["status"], "running")

        release.set()
        completed = manager.collect_subagent(fork_id, wait=True, timeout_seconds=1)

        self.assertEqual(
            completed,
            {
                "ok": True,
                "fork_id": fork_id,
                "status": "completed",
                "output": "finished: background work",
            },
        )
        self.assertEqual(manager.collect_subagent(fork_id)["status"], "cleaned")
        self.assertEqual(
            manager.collect_subagent("never-created")["status"],
            "unknown",
        )
        self.assertEqual(len(closed), 1)

    def test_failed_fork_returns_only_a_bounded_error_summary(self) -> None:
        closed: list[_StubAgent] = []

        def fail(_task: str) -> str:
            raise RuntimeError("child exploded")

        manager = SubAgentManager(lambda: _StubAgent(fail, closed), max_workers=1)
        self.addCleanup(manager.close)

        forked = manager.fork_subagent("fail")
        failed = manager.collect_subagent(
            forked["fork_id"],
            wait=True,
            timeout_seconds=1,
        )

        self.assertFalse(failed["ok"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["code"], "subagent_failed")
        self.assertEqual(failed["error"], "RuntimeError: child exploded")
        self.assertNotIn("traceback", failed)
        self.assertEqual(len(closed), 1)

    def test_multiple_forks_keep_agents_status_and_history_isolated(self) -> None:
        agents: list[_StubAgent] = []
        closed: list[_StubAgent] = []

        def factory() -> _StubAgent:
            agent = _StubAgent(lambda task: task.upper(), closed)
            agents.append(agent)
            return agent

        manager = SubAgentManager(factory, max_workers=2, max_tasks=2)
        self.addCleanup(manager.close)

        first = manager.fork_subagent("first")
        second = manager.fork_subagent("second")
        first_result = manager.collect_subagent(
            first["fork_id"], wait=True, timeout_seconds=1
        )
        second_result = manager.collect_subagent(
            second["fork_id"], wait=True, timeout_seconds=1
        )

        self.assertNotEqual(first["fork_id"], second["fork_id"])
        self.assertEqual(first_result["output"], "FIRST")
        self.assertEqual(second_result["output"], "SECOND")
        self.assertEqual(len(agents), 2)
        self.assertIsNot(agents[0], agents[1])
        self.assertEqual(
            {tuple(agent.history) for agent in agents},
            {("first",), ("second",)},
        )

    def test_worker_and_outstanding_task_limits_are_enforced(self) -> None:
        started = [threading.Event(), threading.Event()]
        releases = [threading.Event(), threading.Event()]
        factory_lock = threading.Lock()
        next_index = 0
        closed: list[_StubAgent] = []

        def factory() -> _StubAgent:
            nonlocal next_index
            with factory_lock:
                index = next_index
                next_index += 1

            def run(task: str) -> str:
                started[index].set()
                if not releases[index].wait(2):
                    raise RuntimeError("test release was not signaled")
                return task

            return _StubAgent(run, closed)

        manager = SubAgentManager(factory, max_workers=1, max_tasks=2)
        self.addCleanup(manager.close)

        first = manager.fork_subagent("first")
        second = manager.fork_subagent("second")
        rejected = manager.fork_subagent("third")

        self.assertTrue(started[0].wait(1))
        self.assertFalse(started[1].is_set())
        self.assertEqual(rejected["code"], "task_limit_reached")

        releases[0].set()
        manager.collect_subagent(first["fork_id"], wait=True, timeout_seconds=1)
        self.assertTrue(started[1].wait(1))
        releases[1].set()
        manager.collect_subagent(second["fork_id"], wait=True, timeout_seconds=1)
        self.assertEqual(len(closed), 2)

    def test_close_is_idempotent_releases_threads_and_rejects_new_work(self) -> None:
        closed: list[_StubAgent] = []
        manager = SubAgentManager(
            lambda: _StubAgent(lambda task: task, closed),
            max_workers=1,
        )
        forked = manager.fork_subagent("finish")
        manager.collect_subagent(forked["fork_id"], wait=True, timeout_seconds=1)

        manager.close()
        manager.close()

        self.assertEqual(manager.fork_subagent("late")["code"], "manager_closed")
        self.assertTrue(manager._executor._shutdown)
        self.assertTrue(
            all(not thread.is_alive() for thread in manager._executor._threads)
        )
        self.assertEqual(len(closed), 1)


if __name__ == "__main__":
    unittest.main()
