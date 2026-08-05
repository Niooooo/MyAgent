import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from myagent.agent import AgentLoop
from myagent.agent_team import AGENT_TEAM_TOOL_NAMES
from myagent.composition import build_default_components
from tests.fakes import FakeResponses, function_call, response


class AgentTeamTests(unittest.TestCase):
    def wait_for_inbox(
        self,
        root: str,
        member: str,
        count: int,
    ) -> list[dict[str, str]]:
        inbox = Path(root) / ".myagent" / "agent-team" / "inboxes" / member
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            files = sorted(inbox.glob("*.json"))
            if len(files) >= count:
                return [json.loads(path.read_text(encoding="utf-8")) for path in files]
            time.sleep(0.01)
        self.fail(f"Timed out waiting for {count} message(s) in {member} inbox")

    def make_agent(
        self,
        responses: FakeResponses,
        root: str,
        *,
        approval_callback=None,
        allowed_tools=None,
        bash_tool=None,
    ) -> AgentLoop:
        components = build_default_components(
            client=SimpleNamespace(responses=responses),
            cwd=root,
            approval_callback=approval_callback,
            allowed_tools=allowed_tools,
            bash_tool=bash_tool,
        )
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            tool_registry=components.tool_registry,
            hooks=components.hooks,
            context_memory=components.context_memory,
            background_bash_runner=components.background_bash_runner,
            scheduled_task_runtime=components.scheduled_task_runtime,
            inbox_reader=components.inbox_reader,
            close_callback=components.close,
        )
        agent.agent_team_manager = components.agent_team_manager
        self.addCleanup(agent.close)
        return agent

    def test_create_requires_approval_and_commits_no_rejected_state(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            approvals = iter([False, True])
            agent = self.make_agent(
                FakeResponses([]), root, approval_callback=lambda _: next(approvals)
            )
            manager = agent.agent_team_manager

            denied = agent.tool_registry.execute(
                "create_teammate", '{"name":"alice","role":"reviewer"}'
            )
            self.assertEqual(denied["code"], "permission_denied")
            self.assertEqual(manager.member_names, ())
            self.assertFalse(
                (Path(root) / ".myagent/agent-team/inboxes/alice").exists()
            )

            created = agent.tool_registry.execute(
                "create_teammate", '{"name":"alice","role":"reviewer"}'
            )
            self.assertTrue(created["ok"])
            self.assertEqual(manager.member_names, ("alice",))

    def test_member_is_persistent_isolated_and_cannot_forge_creation(self) -> None:
        responses = FakeResponses(
            [
                response([], "alice-one"),
                response([], "alice-two"),
                response([], "bob-one"),
                response(
                    [
                        function_call(
                            "forged",
                            "create_teammate",
                            '{"name":"x","role":"x"}',
                        )
                    ]
                ),
                response([], "forgery handled"),
            ]
        )
        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(responses, root, approval_callback=lambda _: True)
            for name in ("alice", "bob"):
                agent.tool_registry.execute(
                    "create_teammate", json.dumps({"name": name, "role": "worker"})
                )

            first = agent.tool_registry.execute(
                "run_teammate", '{"name":"alice","task":"one"}'
            )
            first_messages = self.wait_for_inbox(root, "main", 1)
            second = agent.tool_registry.execute(
                "run_teammate", '{"name":"alice","task":"two"}'
            )
            second_messages = self.wait_for_inbox(root, "main", 2)
            other = agent.tool_registry.execute(
                "run_teammate", '{"name":"bob","task":"other"}'
            )
            third_messages = self.wait_for_inbox(root, "main", 3)
            forged = agent.tool_registry.execute(
                "run_teammate", '{"name":"alice","task":"forge"}'
            )
            final_messages = self.wait_for_inbox(root, "main", 4)

            self.assertEqual(first["status"], "running")
            self.assertEqual(second["status"], "running")
            self.assertEqual(other["status"], "running")
            self.assertEqual(forged["status"], "running")
            completion_text = "\n".join(
                message["message"] for message in final_messages
            )
            self.assertIn("alice-one", completion_text)
            self.assertIn("alice-two", completion_text)
            self.assertIn("bob-one", completion_text)
            self.assertGreaterEqual(len(second_messages), len(first_messages))
            self.assertGreaterEqual(len(third_messages), len(second_messages))
            self.assertEqual(responses.requests[1]["input"][0]["content"], "one")
            self.assertEqual(responses.requests[1]["input"][-1]["content"], "two")
            self.assertEqual(
                responses.requests[2]["input"],
                [{"role": "user", "content": "other"}],
            )
            member_tools = {item["name"] for item in responses.requests[3]["tools"]}
            self.assertEqual(AGENT_TEAM_TOOL_NAMES & member_tools, {"send_team_message"})
            forged_output = json.loads(responses.requests[4]["input"][-1]["output"])
            self.assertEqual(forged_output["error"], "Unknown tool: create_teammate")
            self.assertIn("forgery handled", completion_text)

    def test_message_sender_inbox_ack_and_extra_request_for_member_and_main(self) -> None:
        responses = FakeResponses(
            [
                response(
                    [
                        function_call(
                            "send_b",
                            "send_team_message",
                            '{"recipient":"bob","message":"hello"}',
                        )
                    ]
                ),
                response([], "sent"),
                response([], "before inbox"),
                response([], "bob handled"),
                response(
                    [
                        function_call(
                            "send_main",
                            "send_team_message",
                            '{"recipient":"main","message":"report"}',
                        )
                    ]
                ),
                response([], "reported"),
                response([], "main before inbox"),
                response([], "main handled"),
            ]
        )
        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(responses, root, approval_callback=lambda _: True)
            for name in ("alice", "bob"):
                agent.tool_registry.execute(
                    "create_teammate", json.dumps({"name": name, "role": "worker"})
                )

            agent.tool_registry.execute("run_teammate", '{"name":"alice","task":"send"}')
            self.wait_for_inbox(root, "main", 1)
            bob_inbox = Path(root) / ".myagent/agent-team/inboxes/bob"
            self.assertEqual(len(list(bob_inbox.glob("*.json"))), 1)
            agent.tool_registry.execute("run_teammate", '{"name":"bob","task":"check"}')
            self.wait_for_inbox(root, "main", 2)
            inbox_item = responses.requests[3]["input"][-1]
            self.assertIn("AGENT_TEAM_INBOX", inbox_item["content"])
            self.assertIn('"sender":"alice"', inbox_item["content"])
            self.assertEqual(list(bob_inbox.glob("*.json")), [])

            agent.tool_registry.execute("run_teammate", '{"name":"alice","task":"report"}')
            self.wait_for_inbox(root, "main", 4)
            answer = agent.run("check messages")
            self.assertEqual(answer, "main handled")
            main_inbox = responses.requests[7]["input"][-1]["content"]
            self.assertIn("AGENT_TEAM_INBOX", main_inbox)
            self.assertIn('"sender":"alice"', main_inbox)

    def test_teammate_sensitive_approval_bubbles_identity_and_call_id(self) -> None:
        responses = FakeResponses(
            [
                response(
                    [
                        function_call(
                            "member_call", "bash", '{"command":"rm note.txt"}'
                        )
                    ]
                ),
                response([], "done"),
            ]
        )
        requests = []
        commands = []
        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(
                responses,
                root,
                approval_callback=lambda request: requests.append(request) or True,
                bash_tool=lambda command: commands.append(command) or {"ok": True},
            )
            agent.tool_registry.execute(
                "create_teammate", '{"name":"alice","role":"operator"}'
            )
            result = agent.tool_registry.execute(
                "run_teammate", '{"name":"alice","task":"remove note"}'
            )
            self.wait_for_inbox(root, "main", 1)

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "running")
            self.assertEqual(commands, ["rm note.txt"])
            self.assertIn("Agent Team member alice", requests[-1].reason)
            self.assertEqual(
                responses.requests[1]["input"][-1]["call_id"], "member_call"
            )

    def test_teammate_sensitive_denial_does_not_execute_handler(self) -> None:
        responses = FakeResponses(
            [
                response(
                    [function_call("denied", "bash", '{"command":"rm note.txt"}')]
                ),
                response([], "denial handled"),
            ]
        )
        commands = []
        approval_count = 0

        def approve_creation_only(_request):
            nonlocal approval_count
            approval_count += 1
            return approval_count == 1

        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(
                responses,
                root,
                approval_callback=approve_creation_only,
                bash_tool=lambda command: commands.append(command) or {"ok": True},
            )
            agent.tool_registry.execute(
                "create_teammate", '{"name":"alice","role":"operator"}'
            )
            agent.tool_registry.execute(
                "run_teammate", '{"name":"alice","task":"remove note"}'
            )
            self.wait_for_inbox(root, "main", 1)

            self.assertEqual(commands, [])
            denied = json.loads(responses.requests[1]["input"][-1]["output"])
            self.assertEqual(denied["code"], "permission_denied")

    def test_run_returns_immediately_on_dedicated_thread_and_rejects_busy(self) -> None:
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        worker_names = []

        def blocking_bash(_command):
            worker_names.append(threading.current_thread().name)
            started.set()
            if not release.wait(2):
                raise RuntimeError("test release was not signaled")
            return {"ok": True}

        responses = FakeResponses(
            [
                response(
                    [function_call("blocking", "bash", '{"command":"pwd"}')]
                ),
                response([], "background complete"),
            ]
        )
        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(
                responses,
                root,
                approval_callback=lambda _: True,
                bash_tool=blocking_bash,
            )
            agent.tool_registry.execute(
                "create_teammate", '{"name":"alice","role":"worker"}'
            )

            receipt = agent.tool_registry.execute(
                "run_teammate", '{"name":"alice","task":"block"}'
            )
            self.assertEqual(receipt["status"], "running")
            self.assertTrue(started.wait(1))
            busy = agent.tool_registry.execute(
                "run_teammate", '{"name":"alice","task":"overlap"}'
            )

            self.assertEqual(busy["code"], "member_busy")
            self.assertTrue(worker_names[0].startswith("myagent-teammate-alice"))
            self.assertNotEqual(worker_names[0], threading.current_thread().name)

            release.set()
            messages = self.wait_for_inbox(root, "main", 1)
            self.assertIn("background complete", messages[0]["message"])

    def test_custom_allowlist_subagent_isolation_and_close(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            restricted = self.make_agent(
                FakeResponses([]), root, allowed_tools={"read_file"}
            )
            self.assertEqual(
                {definition["name"] for definition in restricted.tool_registry.definitions},
                {"read_file"},
            )
            denied = restricted.tool_registry.execute(
                "create_teammate", '{"name":"alice","role":"worker"}'
            )
            self.assertEqual(denied["code"], "permission_denied")

        responses = FakeResponses(
            [
                response(
                    [
                        function_call(
                            "forged",
                            "send_team_message",
                            '{"recipient":"main","message":"x"}',
                        )
                    ]
                ),
                response([], "handled"),
            ]
        )
        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(responses, root, approval_callback=lambda _: True)
            subagent = agent.tool_registry.execute("run_subagent", '{"task":"forge"}')
            child_tools = {item["name"] for item in responses.requests[0]["tools"]}
            self.assertTrue(AGENT_TEAM_TOOL_NAMES.isdisjoint(child_tools))
            self.assertEqual(
                json.loads(responses.requests[1]["input"][-1]["output"])["code"],
                "unknown_tool",
            )
            self.assertTrue(subagent["ok"])

            manager = agent.agent_team_manager
            agent.close()
            self.assertEqual(
                manager.create_teammate("late", "worker")["code"],
                "manager_closed",
            )
            self.assertEqual(
                manager.run_teammate("late", "work")["code"], "manager_closed"
            )


if __name__ == "__main__":
    unittest.main()
