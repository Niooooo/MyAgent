import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from myagent.agent import AgentLoop
from myagent.composition import build_default_components
from myagent.hooks import HookRegistry, PostToolUse, PreToolUse
from myagent.permissions import DefaultPermissionPolicy, PermissionHook, PermissionLevel
from myagent.tasks import (
    CLAIM_TASK_TOOL,
    COMPLETE_TASK_TOOL,
    CREATE_TASK_TOOL,
    GET_TASK_TOOL,
    IS_TASK_EXECUTABLE_TOOL,
    LIST_TASKS_TOOL,
    MAX_TASK_FILE_BYTES,
    TASK_MUTATION_TOOL_NAMES,
    TASK_TOOL_NAMES,
    TaskStore,
    TaskStoreError,
    task_tools,
)
from myagent.tooling import ToolRegistry
from tests.fakes import FakeResponses, function_call, response


class TaskStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.workspace = Path(self.temporary_directory.name)
        self.store = TaskStore(self.workspace)

    def create(
        self,
        name: str = "Task",
        *,
        dependencies: list[str] | None = None,
    ) -> dict[str, object]:
        return self.store.create_task(
            name,
            f"Summary for {name}",
            "testing",
            [] if dependencies is None else dependencies,
        )

    def test_empty_list_does_not_create_storage_directory(self) -> None:
        self.assertEqual(self.store.list_tasks(), [])
        self.assertFalse((self.workspace / ".myagent" / "tasks").exists())

    def test_create_normalizes_fields_and_persists_across_store_instances(self) -> None:
        created = self.store.create_task(
            "  Build graph  ",
            "  Persist the DAG  ",
            "  architecture  ",
            [],
        )

        self.assertRegex(created["id"], r"^[0-9a-f]{32}$")
        self.assertEqual(created["name"], "Build graph")
        self.assertEqual(created["summary"], "Persist the DAG")
        self.assertEqual(created["topic"], "architecture")
        self.assertEqual(created["status"], "pending")
        self.assertIsNone(created["owner"])
        self.assertEqual(created["dependencies"], [])

        reloaded = TaskStore(self.workspace).get_task(created["id"])
        self.assertEqual(reloaded["id"], created["id"])
        self.assertEqual(reloaded["name"], "Build graph")
        self.assertTrue(reloaded["executable"])

    def test_empty_dependencies_are_immediately_executable(self) -> None:
        task = self.create()

        result = self.store.is_task_executable(task["id"])

        self.assertEqual(
            result,
            {
                "id": task["id"],
                "executable": True,
                "reason": "ready",
                "blocking_dependency_ids": [],
            },
        )

    def test_dependency_completion_immediately_unblocks_downstream_task(self) -> None:
        dependency = self.create("Dependency")
        downstream = self.create(
            "Downstream",
            dependencies=[dependency["id"]],
        )

        blocked = self.store.is_task_executable(downstream["id"])
        self.assertFalse(blocked["executable"])
        self.assertEqual(blocked["reason"], "blocked_by_dependencies")
        self.assertEqual(
            blocked["blocking_dependency_ids"],
            [dependency["id"]],
        )

        self.store.claim_task(dependency["id"], "Agent-A")
        self.store.complete_task(dependency["id"], "Agent-A")

        ready = self.store.is_task_executable(downstream["id"])
        self.assertTrue(ready["executable"])
        self.assertEqual(ready["reason"], "ready")
        self.assertEqual(ready["blocking_dependency_ids"], [])

    def test_unknown_and_duplicate_dependencies_preserve_snapshot(self) -> None:
        existing = self.create("Existing")
        task_file = self.workspace / ".myagent" / "tasks" / "tasks.json"
        before = task_file.read_bytes()

        cases = (["f" * 32], [existing["id"], existing["id"]])
        for dependencies in cases:
            with self.subTest(dependencies=dependencies):
                with self.assertRaises(TaskStoreError) as raised:
                    self.create("Invalid", dependencies=dependencies)
                self.assertEqual(raised.exception.code, "invalid_task_dependencies")
                self.assertEqual(task_file.read_bytes(), before)

    def test_corrupt_cycle_is_rejected_without_rewriting_file(self) -> None:
        first_id = "a" * 32
        second_id = "b" * 32
        document = {
            "schema_version": 1,
            "tasks": [
                self._raw_task(first_id, dependencies=[second_id]),
                self._raw_task(second_id, dependencies=[first_id]),
            ],
        }
        task_file = self._write_document(document)
        before = task_file.read_bytes()

        with self.assertRaises(TaskStoreError) as raised:
            self.store.create_task("New", "Summary", "topic", [])

        self.assertEqual(raised.exception.code, "task_store_corrupt")
        self.assertEqual(task_file.read_bytes(), before)

    def test_legal_transitions_and_same_owner_retries_are_idempotent(self) -> None:
        task = self.create()

        claimed = self.store.claim_task(task["id"], "Agent-A")
        claimed_again = self.store.claim_task(task["id"], "Agent-A")
        completed = self.store.complete_task(task["id"], "Agent-A")
        completed_again = self.store.complete_task(task["id"], "Agent-A")

        self.assertTrue(claimed["changed"])
        self.assertEqual(claimed["status"], "in_progress")
        self.assertEqual(claimed["owner"], "Agent-A")
        self.assertFalse(claimed_again["changed"])
        self.assertTrue(completed["changed"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["owner"], "Agent-A")
        self.assertFalse(completed_again["changed"])

    def test_owner_mismatch_and_invalid_transitions_do_not_modify_snapshot(self) -> None:
        pending = self.create("Pending")
        task_file = self.workspace / ".myagent" / "tasks" / "tasks.json"
        before_pending_completion = task_file.read_bytes()

        with self.assertRaises(TaskStoreError) as pending_error:
            self.store.complete_task(pending["id"], "Agent-A")
        self.assertEqual(pending_error.exception.code, "invalid_task_transition")
        self.assertEqual(task_file.read_bytes(), before_pending_completion)

        self.store.claim_task(pending["id"], "Agent-A")
        claimed_bytes = task_file.read_bytes()
        operations = (
            (self.store.claim_task, (pending["id"], "Agent-B"), "task_already_claimed"),
            (self.store.complete_task, (pending["id"], "agent-a"), "task_owner_mismatch"),
        )
        for operation, arguments, code in operations:
            with self.subTest(code=code):
                with self.assertRaises(TaskStoreError) as raised:
                    operation(*arguments)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(task_file.read_bytes(), claimed_bytes)

        self.store.complete_task(pending["id"], "Agent-A")
        completed_bytes = task_file.read_bytes()
        with self.assertRaises(TaskStoreError) as completed_error:
            self.store.claim_task(pending["id"], "Agent-A")
        self.assertEqual(completed_error.exception.code, "invalid_task_transition")
        self.assertEqual(task_file.read_bytes(), completed_bytes)

    def test_two_threads_competing_to_claim_have_exactly_one_winner(self) -> None:
        task = self.create()
        barrier = threading.Barrier(2)

        def claim(owner: str) -> tuple[str, bool, str | None]:
            barrier.wait()
            try:
                result = self.store.claim_task(task["id"], owner)
            except TaskStoreError as exc:
                return owner, False, exc.code
            return owner, bool(result["changed"]), None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, ["Agent-A", "Agent-B"]))

        winners = [owner for owner, changed, code in results if changed and code is None]
        losers = [code for _, changed, code in results if not changed]
        self.assertEqual(len(winners), 1)
        self.assertEqual(losers, ["task_already_claimed"])
        self.assertEqual(self.store.get_task(task["id"])["owner"], winners[0])

    def test_list_is_sorted_summary_and_get_contains_full_dependencies(self) -> None:
        first = self.create("First")
        second = self.create("Second", dependencies=[first["id"]])

        listed = self.store.list_tasks()
        detail = self.store.get_task(second["id"])

        self.assertEqual(
            [item["id"] for item in listed],
            sorted([first["id"], second["id"]]),
        )
        self.assertNotIn("dependencies", listed[0])
        self.assertNotIn("path", listed[0])
        self.assertEqual(
            {
                "id",
                "name",
                "status",
                "summary",
                "topic",
                "owner",
                "executable",
                "reason",
                "dependency_count",
                "blocking_dependency_ids",
            },
            set(listed[0]),
        )
        self.assertEqual(detail["dependencies"], [first["id"]])
        self.assertEqual(detail["blocking_dependency_ids"], [first["id"]])

    def test_replace_failure_preserves_old_snapshot_and_cleans_temp_file(self) -> None:
        existing = self.create("Existing")
        task_file = self.workspace / ".myagent" / "tasks" / "tasks.json"
        before = task_file.read_bytes()

        with patch("myagent.tasks.os.replace", side_effect=OSError("private path")):
            with self.assertRaises(TaskStoreError) as raised:
                self.create("Will fail")

        self.assertEqual(raised.exception.code, "task_storage_error")
        self.assertNotIn(str(self.workspace), raised.exception.error)
        self.assertNotIn("private path", raised.exception.error)
        self.assertEqual(task_file.read_bytes(), before)
        self.assertEqual(list(task_file.parent.glob(".tasks.json.*.tmp")), [])
        self.assertEqual(TaskStore(self.workspace).get_task(existing["id"])["name"], "Existing")

    def test_corrupt_oversized_and_schema_mismatched_files_use_stable_error(self) -> None:
        cases = {
            "invalid_json": b"{not-json",
            "invalid_utf8": b"\xff",
            "wrong_schema": json.dumps(
                {"schema_version": 2, "tasks": []}
            ).encode("utf-8"),
            "oversized": b"x" * (MAX_TASK_FILE_BYTES + 1),
        }
        for name, content in cases.items():
            with self.subTest(name=name):
                root = self.workspace / name
                path = root / ".myagent" / "tasks" / "tasks.json"
                path.parent.mkdir(parents=True)
                path.write_bytes(content)
                with self.assertRaises(TaskStoreError) as raised:
                    TaskStore(root).list_tasks()
                self.assertEqual(raised.exception.code, "task_store_corrupt")
                self.assertNotIn(str(root), raised.exception.error)

    def test_tool_schemas_are_strict_and_errors_are_structured(self) -> None:
        tools = task_tools(self.store)

        self.assertEqual({tool.name for tool in tools}, TASK_TOOL_NAMES)
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertTrue(tool.strict)
                self.assertFalse(tool.parameters["additionalProperties"])

        get_task = next(tool for tool in tools if tool.name == "get_task")
        result = get_task.handler(id="f" * 32)
        self.assertEqual(
            result,
            {"ok": False, "code": "task_not_found", "error": "Task does not exist"},
        )

    def _write_document(self, document: dict[str, object]) -> Path:
        task_file = self.workspace / ".myagent" / "tasks" / "tasks.json"
        task_file.parent.mkdir(parents=True, exist_ok=True)
        task_file.write_text(json.dumps(document), encoding="utf-8")
        return task_file

    @staticmethod
    def _raw_task(
        task_id: str,
        *,
        dependencies: list[str] | None = None,
    ) -> dict[str, object]:
        return {
            "id": task_id,
            "name": "Task",
            "status": "pending",
            "summary": "Summary",
            "topic": "testing",
            "owner": None,
            "dependencies": [] if dependencies is None else dependencies,
        }


class TaskIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.workspace = Path(self.temporary_directory.name)

    def test_three_mutations_require_per_call_approval_and_reads_are_allowed(self) -> None:
        policy = DefaultPermissionPolicy()
        read_tools = {
            IS_TASK_EXECUTABLE_TOOL,
            LIST_TASKS_TOOL,
            GET_TASK_TOOL,
        }

        for tool_name in TASK_MUTATION_TOOL_NAMES:
            with self.subTest(tool_name=tool_name):
                self.assertEqual(
                    policy.evaluate(tool_name, {}).level,
                    PermissionLevel.REQUIRE_APPROVAL,
                )
        for tool_name in read_tools:
            with self.subTest(tool_name=tool_name):
                self.assertEqual(
                    policy.evaluate(tool_name, {}).level,
                    PermissionLevel.ALLOW,
                )

        approvals: list[str] = []
        components = build_default_components(
            cwd=self.workspace,
            bash_tool=lambda command: {"ok": True},
            approval_callback=lambda request: approvals.append(request.tool_name)
            or True,
        )
        created = components.tool_registry.execute(
            CREATE_TASK_TOOL,
            json.dumps(
                {
                    "name": "Approved",
                    "summary": "Exercise task approvals",
                    "topic": "permissions",
                    "dependencies": [],
                }
            ),
        )
        task_id = created["id"]
        components.tool_registry.execute(
            CLAIM_TASK_TOOL,
            json.dumps({"id": task_id, "owner": "Agent-A"}),
        )
        components.tool_registry.execute(
            COMPLETE_TASK_TOOL,
            json.dumps({"id": task_id, "owner": "Agent-A"}),
        )
        components.tool_registry.execute(
            IS_TASK_EXECUTABLE_TOOL,
            json.dumps({"id": task_id}),
        )
        components.tool_registry.execute(LIST_TASKS_TOOL, "{}")
        components.tool_registry.execute(
            GET_TASK_TOOL,
            json.dumps({"id": task_id}),
        )

        self.assertEqual(
            approvals,
            [CREATE_TASK_TOOL, CLAIM_TASK_TOOL, COMPLETE_TASK_TOOL],
        )

    def test_rejected_mutation_does_not_create_task_storage(self) -> None:
        components = build_default_components(
            cwd=self.workspace,
            bash_tool=lambda command: {"ok": True},
        )

        denied = components.tool_registry.execute(
            CREATE_TASK_TOOL,
            json.dumps(
                {
                    "name": "Denied",
                    "summary": "Must not be persisted",
                    "topic": "permissions",
                    "dependencies": [],
                }
            ),
        )
        listed = components.tool_registry.execute(LIST_TASKS_TOOL, "{}")

        self.assertEqual(denied["code"], "permission_denied")
        self.assertEqual(listed, {"ok": True, "tasks": [], "count": 0})
        self.assertFalse((self.workspace / ".myagent" / "tasks").exists())

    def test_custom_allowlist_hides_and_rejects_task_tools(self) -> None:
        components = build_default_components(
            cwd=self.workspace,
            bash_tool=lambda command: {"ok": True},
            allowed_tools={LIST_TASKS_TOOL},
        )

        visible = {
            definition["name"] for definition in components.tool_registry.definitions
        }
        forged = components.tool_registry.execute(
            CREATE_TASK_TOOL,
            json.dumps(
                {
                    "name": "Hidden",
                    "summary": "Must remain hidden",
                    "topic": "permissions",
                    "dependencies": [],
                }
            ),
        )

        self.assertEqual(visible, {LIST_TASKS_TOOL})
        self.assertEqual(forged["code"], "permission_denied")
        self.assertIn("allowlist", forged["error"])
        self.assertFalse((self.workspace / ".myagent" / "tasks").exists())

    def test_all_six_tools_cross_hooks_and_keep_registry_call_ids(self) -> None:
        hooks = HookRegistry()
        pre_events: list[tuple[str, str | None]] = []
        post_events: list[tuple[str, str | None]] = []
        hooks.register(
            PreToolUse,
            lambda event: pre_events.append((event.tool_name, event.call_id)),
        )
        hooks.register(
            PostToolUse,
            lambda event: post_events.append((event.tool_name, event.call_id)),
        )
        components = build_default_components(
            cwd=self.workspace,
            bash_tool=lambda command: {"ok": True},
            approval_callback=lambda request: True,
            hooks=hooks,
        )
        self.assertIsInstance(hooks.handlers_for(PreToolUse)[0], PermissionHook)

        calls: list[tuple[str, str, str]] = []
        create_arguments = json.dumps(
            {
                "name": "Hooked",
                "summary": "Observe every tool",
                "topic": "hooks",
                "dependencies": [],
            }
        )
        created = components.tool_registry.execute(
            CREATE_TASK_TOOL,
            create_arguments,
            call_id="task-call-1",
        )
        task_id = created["id"]
        calls.extend(
            [
                (CREATE_TASK_TOOL, create_arguments, "task-call-1"),
                (
                    IS_TASK_EXECUTABLE_TOOL,
                    json.dumps({"id": task_id}),
                    "task-call-2",
                ),
                (
                    CLAIM_TASK_TOOL,
                    json.dumps({"id": task_id, "owner": "Agent-A"}),
                    "task-call-3",
                ),
                (
                    COMPLETE_TASK_TOOL,
                    json.dumps({"id": task_id, "owner": "Agent-A"}),
                    "task-call-4",
                ),
                (LIST_TASKS_TOOL, "{}", "task-call-5"),
                (GET_TASK_TOOL, json.dumps({"id": task_id}), "task-call-6"),
            ]
        )
        for tool_name, arguments, call_id in calls[1:]:
            result = components.tool_registry.execute(
                tool_name,
                arguments,
                call_id=call_id,
            )
            self.assertTrue(result["ok"], result)

        expected = [(name, call_id) for name, _, call_id in calls]
        self.assertEqual(pre_events, expected)
        self.assertEqual(post_events, expected)

    def test_agent_loop_returns_task_output_with_original_call_id(self) -> None:
        responses = FakeResponses(
            [
                response(
                    [
                        function_call(
                            call_id="task-list-call",
                            name=LIST_TASKS_TOOL,
                            arguments="{}",
                        )
                    ]
                ),
                response([], "done"),
            ]
        )
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            workspace_root=self.workspace,
        )
        self.addCleanup(agent.close)

        self.assertEqual(agent.run("List persistent tasks"), "done")

        output = next(
            item
            for item in agent.history
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        )
        self.assertEqual(output["call_id"], "task-list-call")
        self.assertEqual(json.loads(output["output"])["tasks"], [])

    def test_parent_and_child_share_task_store_but_not_session_state(self) -> None:
        client = SimpleNamespace(responses=FakeResponses([]))
        injected = TaskStore(self.workspace)
        components = build_default_components(
            client=client,
            cwd=self.workspace,
            bash_tool=lambda command: {"ok": True},
            task_store=injected,
        )
        self.addCleanup(components.close)
        run_tool = components.tool_registry._tools["run_subagent"]
        manager = run_tool.handler.__self__
        child = manager._agent_factory()
        self.addCleanup(child.close)

        parent_names = {
            definition["name"] for definition in components.tool_registry.definitions
        }
        child_names = {
            definition["name"] for definition in child.tool_registry.definitions
        }
        self.assertIs(components.task_store, injected)
        self.assertIs(child.task_store, injected)
        self.assertTrue(TASK_TOOL_NAMES.issubset(parent_names))
        self.assertTrue(TASK_TOOL_NAMES.issubset(child_names))
        self.assertNotIn("run_subagent", child_names)
        self.assertIsNot(child.todo_list, components.todo_list)
        self.assertIsNot(child.hooks, components.hooks)
        self.assertIsNot(child.context_memory, components.context_memory)

    def test_injected_store_must_match_workspace(self) -> None:
        other_directory = TemporaryDirectory()
        self.addCleanup(other_directory.cleanup)

        with self.assertRaisesRegex(ValueError, "task_store"):
            build_default_components(
                cwd=self.workspace,
                bash_tool=lambda command: {"ok": True},
                task_store=TaskStore(other_directory.name),
            )

    def test_reset_and_agent_recreation_preserve_tasks(self) -> None:
        client = SimpleNamespace(responses=FakeResponses([]))
        first = AgentLoop(
            client,
            workspace_root=self.workspace,
            approval_callback=lambda request: True,
        )
        created = first.tool_registry.execute(
            CREATE_TASK_TOOL,
            json.dumps(
                {
                    "name": "Persistent",
                    "summary": "Survive reset and recreation",
                    "topic": "lifecycle",
                    "dependencies": [],
                }
            ),
        )
        task_id = created["id"]

        first.reset()
        self.assertTrue(
            first.tool_registry.execute(
                GET_TASK_TOOL,
                json.dumps({"id": task_id}),
            )["ok"]
        )
        first.close()

        second = AgentLoop(client, workspace_root=self.workspace)
        self.addCleanup(second.close)
        reloaded = second.tool_registry.execute(
            GET_TASK_TOOL,
            json.dumps({"id": task_id}),
        )
        self.assertEqual(reloaded["name"], "Persistent")
        self.assertIsNotNone(second.task_store)

    def test_explicit_custom_registry_does_not_create_default_task_runtime(self) -> None:
        agent = AgentLoop(
            SimpleNamespace(responses=FakeResponses([])),
            tool_registry=ToolRegistry(),
        )

        self.assertIsNone(agent.task_store)
        self.assertEqual(agent.tool_registry.definitions, [])
        self.assertFalse((self.workspace / ".myagent" / "tasks").exists())


if __name__ == "__main__":
    unittest.main()
