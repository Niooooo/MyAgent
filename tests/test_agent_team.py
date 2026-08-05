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
from myagent.tasks import TaskStore
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
            self.assertEqual(
                AGENT_TEAM_TOOL_NAMES & member_tools,
                {"send_team_message", "request_plan_approval"},
            )
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

    def test_shutdown_requires_approval_and_waits_for_busy_member(self) -> None:
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        approvals = iter([True, False, True])
        responses = FakeResponses(
            [
                response([function_call("blocking", "bash", '{"command":"pwd"}')]),
                response([], "finished first"),
            ]
        )

        def blocking_bash(_command):
            started.set()
            release.wait(2)
            return {"ok": True}

        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(
                responses,
                root,
                approval_callback=lambda _: next(approvals),
                bash_tool=blocking_bash,
            )
            manager = agent.agent_team_manager
            agent.tool_registry.execute(
                "create_teammate", '{"name":"alice","role":"worker"}'
            )
            denied = agent.tool_registry.execute(
                "request_teammate_shutdown",
                '{"name":"alice","reason":"done"}',
            )
            self.assertEqual(denied["code"], "permission_denied")
            self.assertEqual(manager.member_names, ("alice",))

            agent.tool_registry.execute(
                "run_teammate", '{"name":"alice","task":"finish"}'
            )
            self.assertTrue(started.wait(1))
            requested = agent.tool_registry.execute(
                "request_teammate_shutdown",
                '{"name":"alice","reason":"done"}',
            )
            duplicate = manager.request_teammate_shutdown("alice", "again")
            self.assertEqual(duplicate["request_id"], requested["request_id"])
            self.assertTrue(duplicate["duplicate"])
            rejected = agent.tool_registry.execute(
                "run_teammate", '{"name":"alice","task":"late"}'
            )
            self.assertEqual(rejected["code"], "member_shutting_down")

            release.set()
            messages = self.wait_for_inbox(root, "main", 2)
            completion = next(
                item["message"]
                for item in messages
                if item["message"].startswith("AGENT_TEAM_TASK_RESULT")
            )
            shutdown = next(
                item["message"]
                for item in messages
                if item["message"].startswith("AGENT_TEAM_PROTOCOL")
            )
            self.assertNotIn("next_task_claim", completion)
            protocol = json.loads(shutdown.split("\n", 1)[1])
            self.assertEqual(protocol["type"], "shutdown_completed")
            self.assertEqual(protocol["request_id"], requested["request_id"])
            self.assertEqual(manager.member_names, ())

    def test_plan_approval_round_trip_and_failed_review_keeps_pending(self) -> None:
        responses = FakeResponses(
            [response([], "before response"), response([], "response received")]
        )
        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(responses, root, approval_callback=lambda _: True)
            manager = agent.agent_team_manager
            task = agent.tool_registry.execute(
                "create_task",
                json.dumps(
                    {
                        "name": "inspect",
                        "summary": "inspect the implementation",
                        "topic": "team",
                        "dependencies": [],
                    }
                ),
            )
            agent.tool_registry.execute(
                "create_teammate", '{"name":"alice","role":"worker"}'
            )
            teammate_registry = manager._members["alice"].agent.tool_registry
            original_writer = manager._write_message
            manager._write_message = lambda *_: {
                "ok": False,
                "code": "message_write_failed",
                "error": "Agent Team message could not be saved",
            }
            failed_request = manager.request_plan_approval(
                "alice", task["id"], "not saved"
            )
            self.assertEqual(failed_request["code"], "message_write_failed")
            self.assertEqual(manager._pending_plan_approvals, {})
            manager._write_message = original_writer
            self.assertEqual(
                agent.tool_registry.execute(
                    "request_plan_approval",
                    json.dumps({"task_id": task["id"], "plan": "forged"}),
                )["code"],
                "unknown_tool",
            )
            requested = teammate_registry.execute(
                "request_plan_approval",
                json.dumps({"task_id": task["id"], "plan": "inspect first"}),
            )
            self.assertFalse(requested["duplicate"])
            duplicate = teammate_registry.execute(
                "request_plan_approval",
                json.dumps({"task_id": task["id"], "plan": "duplicate"}),
            )
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(duplicate["request_id"], requested["request_id"])
            messages = self.wait_for_inbox(root, "main", 1)
            request_text = next(
                item["message"]
                for item in messages
                if item["message"].startswith("AGENT_TEAM_PROTOCOL")
            )
            request = json.loads(request_text.split("\n", 1)[1])
            self.assertEqual(request["type"], "plan_approval_request")
            self.assertEqual(request["member"], "alice")
            self.assertEqual(request["task_id"], task["id"])

            manager._write_message = lambda *_: {
                "ok": False,
                "code": "message_write_failed",
                "error": "Agent Team message could not be saved",
            }
            failed = agent.tool_registry.execute(
                "review_teammate_plan",
                json.dumps(
                    {
                        "request_id": request["request_id"],
                        "approved": True,
                        "feedback": "go",
                    }
                ),
            )
            self.assertEqual(failed["code"], "message_write_failed")
            self.assertIn(request["request_id"], manager._pending_plan_approvals)
            self.assertIsNone(manager._members["alice"].queued_plan_request_id)
            self.assertEqual(responses.requests, [])
            manager._write_message = original_writer

            reviewed = agent.tool_registry.execute(
                "review_teammate_plan",
                json.dumps(
                    {
                        "request_id": request["request_id"],
                        "approved": False,
                        "feedback": "not yet",
                    }
                ),
            )
            self.assertTrue(reviewed["ok"])
            self.assertEqual(reviewed["status"], "rejected")
            repeated = agent.tool_registry.execute(
                "review_teammate_plan",
                json.dumps(
                    {
                        "request_id": request["request_id"],
                        "approved": False,
                        "feedback": "again",
                    }
                ),
            )
            self.assertEqual(repeated["code"], "pending_plan_not_found")
            agent.tool_registry.execute(
                "run_teammate", '{"name":"alice","task":"continue"}'
            )
            self.wait_for_inbox(root, "main", 2)
            inbox_content = responses.requests[1]["input"][-1]["content"]
            self.assertIn("plan_approval_response", inbox_content)
            self.assertIn(request["request_id"], inbox_content)
            persisted = agent.tool_registry.execute(
                "get_task", json.dumps({"id": task["id"]})
            )
            self.assertEqual(persisted["status"], "pending")

    def test_idle_shutdown_cleans_pending_plan_requests(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(
                FakeResponses([]), root, approval_callback=lambda _: True
            )
            manager = agent.agent_team_manager
            task = agent.tool_registry.execute(
                "create_task",
                json.dumps(
                    {
                        "name": "inspect",
                        "summary": "inspect",
                        "topic": "team",
                        "dependencies": [],
                    }
                ),
            )
            agent.tool_registry.execute(
                "create_teammate", '{"name":"alice","role":"worker"}'
            )
            pending = manager.request_plan_approval(
                "alice", task["id"], "inspect"
            )
            self.assertIn(pending["request_id"], manager._pending_plan_approvals)
            requested = agent.tool_registry.execute(
                "request_teammate_shutdown",
                '{"name":"alice","reason":"idle"}',
            )
            self.assertTrue(requested["ok"])
            self.wait_for_inbox(root, "main", 2)
            self.assertEqual(manager.member_names, ())
            self.assertEqual(manager._pending_plan_approvals, {})
            repeated = manager.request_teammate_shutdown("alice", "again")
            self.assertTrue(repeated["duplicate"])
            self.assertEqual(repeated["request_id"], requested["request_id"])
            self.assertEqual(repeated["status"], "shutdown_completed")

    def test_approved_plan_claims_executes_and_completes_through_teammate(self) -> None:
        approval_requests = []
        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(
                FakeResponses(
                    [
                        response([], "task executed"),
                        response([], "approval acknowledged"),
                    ]
                ),
                root,
                approval_callback=lambda request: approval_requests.append(request)
                or True,
            )
            created = agent.tool_registry.execute(
                "create_task",
                json.dumps(
                    {
                        "name": "approved work",
                        "summary": "execute after main approval",
                        "topic": "team",
                        "dependencies": [],
                    }
                ),
            )
            agent.tool_registry.execute(
                "create_teammate", '{"name":"alice","role":"worker"}'
            )
            manager = agent.agent_team_manager
            teammate_registry = manager._members["alice"].agent.tool_registry
            requested = teammate_registry.execute(
                "request_plan_approval",
                json.dumps(
                    {"task_id": created["id"], "plan": "implement and verify"}
                ),
            )
            reviewed = agent.tool_registry.execute(
                "review_teammate_plan",
                json.dumps(
                    {
                        "request_id": requested["request_id"],
                        "approved": True,
                        "feedback": "proceed",
                    }
                ),
            )
            self.assertEqual(reviewed["status"], "execution_queued")
            repeated = agent.tool_registry.execute(
                "review_teammate_plan",
                json.dumps(
                    {
                        "request_id": requested["request_id"],
                        "approved": True,
                        "feedback": "again",
                    }
                ),
            )
            self.assertEqual(repeated["code"], "pending_plan_not_found")

            messages = self.wait_for_inbox(root, "main", 2)
            completion = next(
                item["message"]
                for item in messages
                if item["message"].startswith("AGENT_TEAM_TASK_RESULT")
            )
            result = json.loads(completion.split("\n", 1)[1])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["persistent_task_id"], created["id"])
            self.assertEqual(result["plan_request_id"], requested["request_id"])
            self.assertTrue(result["claim"]["ok"])
            self.assertTrue(result["task_completion"]["ok"])
            task = agent.tool_registry.execute(
                "get_task", json.dumps({"id": created["id"]})
            )
            self.assertEqual(task["owner"], "alice")
            self.assertEqual(task["status"], "completed")
            self.assertTrue(
                any(
                    isinstance(item, dict)
                    and "AGENT_TEAM_APPROVED_TASK" in item.get("content", "")
                    for item in manager._members["alice"].agent.history
                )
            )
            for tool_name in ("claim_task", "complete_task"):
                approval = next(
                    request
                    for request in approval_requests
                    if request.tool_name == tool_name
                )
                self.assertIn("Agent Team member alice", approval.reason)

    def test_claim_requires_main_approval_and_user_denial_prevents_execution(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            deny_claim = False

            def approve(request):
                return not (deny_claim and request.tool_name == "claim_task")

            agent = self.make_agent(
                FakeResponses([]),
                root,
                approval_callback=approve,
            )
            created = agent.tool_registry.execute(
                "create_task",
                json.dumps(
                    {
                        "name": "next",
                        "summary": "next",
                        "topic": "team",
                        "dependencies": [],
                    }
                ),
            )
            agent.tool_registry.execute(
                "create_teammate", '{"name":"alice","role":"worker"}'
            )
            manager = agent.agent_team_manager
            teammate_registry = manager._members["alice"].agent.tool_registry
            direct = teammate_registry.execute(
                "claim_task",
                json.dumps({"id": created["id"], "owner": "alice"}),
            )
            self.assertEqual(direct["code"], "plan_approval_required")

            requested = teammate_registry.execute(
                "request_plan_approval",
                json.dumps({"task_id": created["id"], "plan": "execute next"}),
            )
            deny_claim = True
            reviewed = agent.tool_registry.execute(
                "review_teammate_plan",
                json.dumps(
                    {
                        "request_id": requested["request_id"],
                        "approved": True,
                        "feedback": "go",
                    }
                ),
            )
            self.assertTrue(reviewed["ok"])
            messages = self.wait_for_inbox(root, "main", 2)
            completion = next(
                item["message"]
                for item in messages
                if item["message"].startswith("AGENT_TEAM_TASK_RESULT")
            )
            result = json.loads(completion.split("\n", 1)[1])
            self.assertEqual(result["status"], "claim_failed")
            self.assertEqual(result["claim"]["code"], "permission_denied")
            persisted = agent.tool_registry.execute(
                "get_task", json.dumps({"id": created["id"]})
            )
            self.assertEqual(persisted["status"], "pending")
            self.assertEqual(manager._members["alice"].agent.client.responses.requests, [])

    def test_plan_request_allowlist_and_approved_claim_race_are_non_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            created = TaskStore(root).create_task("next", "next", "team", [])
            restricted = self.make_agent(
                FakeResponses([]),
                root,
                approval_callback=lambda _: True,
                allowed_tools={
                    "create_teammate",
                    "run_teammate",
                    "send_team_message",
                    "request_plan_approval",
                },
            )
            restricted.tool_registry.execute(
                "create_teammate", '{"name":"alice","role":"worker"}'
            )
            teammate_registry = (
                restricted.agent_team_manager._members["alice"].agent.tool_registry
            )
            denied = teammate_registry.execute(
                "request_plan_approval",
                json.dumps({"task_id": created["id"], "plan": "work"}),
            )
            self.assertEqual(denied["code"], "permission_denied")
            self.assertEqual(
                restricted.agent_team_manager._pending_plan_approvals, {}
            )

        with tempfile.TemporaryDirectory() as root:
            created_id = None

            def race_before_claim(request):
                if request.tool_name == "claim_task":
                    TaskStore(root).claim_task(created_id, "racer")
                return True

            agent = self.make_agent(
                FakeResponses([]),
                root,
                approval_callback=race_before_claim,
            )
            created = agent.tool_registry.execute(
                "create_task",
                json.dumps(
                    {
                        "name": "next",
                        "summary": "next",
                        "topic": "team",
                        "dependencies": [],
                    }
                ),
            )
            created_id = created["id"]
            agent.tool_registry.execute(
                "create_teammate", '{"name":"alice","role":"worker"}'
            )
            manager = agent.agent_team_manager
            teammate_registry = manager._members["alice"].agent.tool_registry
            requested = teammate_registry.execute(
                "request_plan_approval",
                json.dumps({"task_id": created_id, "plan": "race safely"}),
            )
            agent.tool_registry.execute(
                "review_teammate_plan",
                json.dumps(
                    {
                        "request_id": requested["request_id"],
                        "approved": True,
                        "feedback": "go",
                    }
                ),
            )
            messages = self.wait_for_inbox(root, "main", 2)
            completion = next(
                item["message"]
                for item in messages
                if item["message"].startswith("AGENT_TEAM_TASK_RESULT")
            )
            result = json.loads(completion.split("\n", 1)[1])
            self.assertEqual(result["status"], "claim_failed")
            task = agent.tool_registry.execute(
                "get_task", json.dumps({"id": created_id})
            )
            self.assertEqual(task["owner"], "racer")

    def test_plan_request_rejects_non_executable_task(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(
                FakeResponses([]),
                root,
                approval_callback=lambda _: True,
            )
            created = agent.tool_registry.execute(
                "create_task",
                json.dumps(
                    {
                        "name": "already owned",
                        "summary": "not executable",
                        "topic": "team",
                        "dependencies": [],
                    }
                ),
            )
            agent.tool_registry.execute(
                "claim_task",
                json.dumps({"id": created["id"], "owner": "main"}),
            )
            agent.tool_registry.execute(
                "create_teammate", '{"name":"alice","role":"worker"}'
            )
            manager = agent.agent_team_manager
            requested = manager._members["alice"].agent.tool_registry.execute(
                "request_plan_approval",
                json.dumps({"task_id": created["id"], "plan": "take over"}),
            )
            self.assertEqual(requested["code"], "task_not_executable")
            self.assertEqual(manager._pending_plan_approvals, {})

    def test_shutdown_after_approval_before_claim_skips_execution(self) -> None:
        claim_reached = threading.Event()
        release_claim = threading.Event()
        self.addCleanup(release_claim.set)
        with tempfile.TemporaryDirectory() as root:
            agent = self.make_agent(
                FakeResponses([]),
                root,
                approval_callback=lambda _: True,
            )
            created = agent.tool_registry.execute(
                "create_task",
                json.dumps(
                    {
                        "name": "next",
                        "summary": "next",
                        "topic": "team",
                        "dependencies": [],
                    }
                ),
            )
            agent.tool_registry.execute(
                "create_teammate", '{"name":"alice","role":"worker"}'
            )
            member = agent.agent_team_manager._members["alice"]
            requested = member.agent.tool_registry.execute(
                "request_plan_approval",
                json.dumps({"task_id": created["id"], "plan": "execute safely"}),
            )
            original_execute = member.agent.tool_registry.execute

            def gated_execute(name, raw_arguments, *, call_id=None):
                if name == "claim_task":
                    claim_reached.set()
                    if not release_claim.wait(2):
                        raise RuntimeError("claim gate was not released")
                return original_execute(name, raw_arguments, call_id=call_id)

            member.agent.tool_registry.execute = gated_execute
            agent.tool_registry.execute(
                "review_teammate_plan",
                json.dumps(
                    {
                        "request_id": requested["request_id"],
                        "approved": True,
                        "feedback": "go",
                    }
                ),
            )
            self.assertTrue(claim_reached.wait(1))
            shutdown = agent.tool_registry.execute(
                "request_teammate_shutdown",
                '{"name":"alice","reason":"stop before claim"}',
            )
            self.assertTrue(shutdown["ok"])
            self.assertEqual(shutdown["status"], "shutting_down")
            release_claim.set()

            messages = self.wait_for_inbox(root, "main", 3)
            completion = next(
                message["message"]
                for message in messages
                if message["message"].startswith("AGENT_TEAM_TASK_RESULT")
            )
            result = json.loads(completion.split("\n", 1)[1])
            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "member_shutting_down")
            task = agent.tool_registry.execute(
                "get_task", json.dumps({"id": created["id"]})
            )
            self.assertEqual(task["status"], "pending")
            self.assertIsNone(task["owner"])


if __name__ == "__main__":
    unittest.main()
