from __future__ import annotations

import json
import threading
import unittest
from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from myagent.agent import AgentLoop
from myagent.composition import (
    build_default_components,
    build_default_tool_registry,
    create_default_agent,
)
from myagent.hooks import HookRegistry, PostToolUse, PreToolUse
from myagent.scheduled_tasks import (
    DELETE_SCHEDULED_TASK_TOOL,
    ONE_TIME_SCHEDULE,
    REGISTER_ONE_TIME_TASK_TOOL,
    REGISTER_SCHEDULED_TASK_TOOL,
    RECURRING_SCHEDULE,
    SCHEDULED_TASK_TOOL_NAMES,
    ScheduledTaskRuntime,
    scheduled_task_tools,
)
from myagent.tooling import ToolRegistry
from tests.fakes import FakeResponses, function_call, response


class _Clock:
    def __init__(self, wall: datetime, monotonic: float = 0.0) -> None:
        self.wall = wall
        self.monotonic = monotonic

    def wall_now(self) -> datetime:
        return self.wall

    def monotonic_now(self) -> float:
        return self.monotonic


_TEST_TIMEZONE = timezone(timedelta(hours=8))


def _local_datetime(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=_TEST_TIMEZONE)


class ScheduledTaskRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock(_local_datetime(2026, 8, 4, 9, 30))

    def runtime(
        self,
        executor=lambda tool_name, tool_arguments: None,
        **overrides,
    ) -> ScheduledTaskRuntime:
        options = {
            "max_jitter_seconds": 0,
            "wall_clock": self.clock.wall_now,
            "monotonic_clock": self.clock.monotonic_now,
            "random_uniform": lambda lower, upper: 0.0,
        }
        options.update(overrides)
        return ScheduledTaskRuntime(
            executor,
            {"bad", "early", "first", "good", "late", "second", "work"},
            **options,
        )

    def test_valid_five_field_cron_registers_unique_normalized_tasks(self) -> None:
        runtime = self.runtime()

        first = runtime.register_scheduled_task(
            "  */5   * * * 1-5 ", "work", '{ "z": 1, "a": "x" }'
        )
        second = runtime.register_scheduled_task("*/5 * * * 1-5", "work", "{}")

        self.assertTrue(first["ok"])
        self.assertEqual(first["cron_expression"], "*/5 * * * 1-5")
        self.assertEqual(first["schedule_type"], RECURRING_SCHEDULE)
        self.assertEqual(first["tool_name"], "work")
        self.assertEqual(first["tool_arguments"], '{"a":"x","z":1}')
        self.assertRegex(first["id"], r"^[0-9a-f]{32}$")
        self.assertNotEqual(first["id"], second["id"])

    def test_one_time_task_requires_future_aware_iso_datetime(self) -> None:
        runtime = self.runtime()
        future = runtime.register_one_time_task(
            "2026-08-04T09:31:05+08:00",
            "work",
            '{ "value": 1 }',
        )

        self.assertTrue(future["ok"])
        self.assertEqual(future["schedule_type"], ONE_TIME_SCHEDULE)
        self.assertEqual(future["run_at"], "2026-08-04T01:31:05Z")
        self.assertEqual(future["tool_arguments"], '{"value":1}')
        self.assertEqual(
            runtime.register_one_time_task(
                "2026-08-04T09:31:05", "work", "{}"
            )["code"],
            "invalid_run_at",
        )
        self.assertEqual(
            runtime.register_one_time_task(
                "2026-08-04T09:29:59+08:00", "work", "{}"
            )["code"],
            "run_at_not_future",
        )
        self.assertEqual(runtime.registered_count, 1)

    def test_one_time_task_enqueues_when_due_and_unregisters_on_start(self) -> None:
        executed: list[tuple[str, str]] = []
        runtime = self.runtime(lambda name, arguments: executed.append((name, arguments)))
        registered = runtime.register_one_time_task(
            "2026-08-04T09:30:05+08:00", "work", '{"once":true}'
        )

        self.assertEqual(runtime.scan_once(), 0)
        self.clock.wall = _local_datetime(2026, 8, 4, 9, 31)
        self.assertEqual(runtime.scan_once(), 1)
        self.assertEqual(runtime.scan_once(), 0)
        self.assertTrue(runtime.run_ready_once())

        self.assertEqual(executed, [("work", '{"once":true}')])
        self.assertEqual(runtime.registered_count, 0)
        self.assertEqual(
            runtime.delete_scheduled_task(registered["id"])["code"],
            "scheduled_task_not_found",
        )

    def test_full_queue_allows_due_one_time_task_to_retry(self) -> None:
        executed: list[str] = []
        runtime = self.runtime(
            lambda name, arguments: executed.append(name),
            max_queued_runs=1,
        )
        runtime.register_scheduled_task("* * * * *", "first", "{}")
        runtime.register_one_time_task(
            "2026-08-04T09:30:01+08:00", "second", "{}"
        )
        self.clock.wall = _local_datetime(2026, 8, 4, 9, 31)

        self.assertEqual(runtime.scan_once(), 1)
        self.assertTrue(runtime.run_ready_once())
        self.assertEqual(runtime.scan_once(), 1)
        self.assertTrue(runtime.run_ready_once())
        self.assertEqual(executed, ["first", "second"])

    def test_invalid_inputs_are_structured_and_never_registered(self) -> None:
        runtime = self.runtime()

        invalid = [
            runtime.register_scheduled_task("* * * * *", "", "{}"),
            runtime.register_scheduled_task("* * * * *", "unknown", "{}"),
            runtime.register_scheduled_task("* * * * *", "work", "not json"),
            runtime.register_scheduled_task("* * * * *", "work", "[]"),
            runtime.register_scheduled_task("not cron", "work", "{}"),
            runtime.register_scheduled_task("* * * * * *", "work", "{}"),
            runtime.register_scheduled_task("@daily", "work", "{}"),
        ]

        self.assertEqual(invalid[0]["code"], "invalid_tool_name")
        self.assertEqual(invalid[1]["code"], "tool_not_schedulable")
        self.assertTrue(
            all(item["code"] == "invalid_tool_arguments" for item in invalid[2:4])
        )
        self.assertTrue(
            all(item["code"] == "invalid_cron_expression" for item in invalid[4:])
        )
        self.assertEqual(
            runtime.delete_scheduled_task("ABC")["code"],
            "invalid_scheduled_task_id",
        )
        self.assertEqual(runtime.registered_count, 0)

    def test_registration_limit_rejects_the_next_task(self) -> None:
        runtime = self.runtime()
        for index in range(100):
            self.assertTrue(
                runtime.register_scheduled_task(
                    "* * * * *", "work", json.dumps({"index": index})
                )["ok"]
            )

        rejected = runtime.register_scheduled_task("* * * * *", "work", "{}")

        self.assertEqual(rejected["code"], "scheduled_task_limit_reached")
        self.assertEqual(runtime.registered_count, 100)

    def test_consumer_passes_saved_tool_name_and_arguments_to_executor(self) -> None:
        executed: list[tuple[str, str]] = []
        runtime = self.runtime(lambda name, arguments: executed.append((name, arguments)))
        runtime.register_scheduled_task(
            "* * * * *", "work", '{ "second": 2, "first": 1 }'
        )

        runtime.scan_once()
        self.assertTrue(runtime.run_ready_once())

        self.assertEqual(executed, [("work", '{"first":1,"second":2}')])

    def test_sixty_scans_enqueue_once_and_next_minute_enqueues_again(self) -> None:
        runtime = self.runtime()
        runtime.register_scheduled_task("* * * * *", "work", "{}")

        self.assertEqual(sum(runtime.scan_once() for _ in range(60)), 1)
        self.assertEqual(runtime.queued_count, 1)
        self.clock.wall = _local_datetime(2026, 8, 4, 9, 31)

        self.assertEqual(runtime.scan_once(), 1)
        self.assertEqual(runtime.queued_count, 2)

    def test_non_matching_task_does_not_enqueue(self) -> None:
        runtime = self.runtime()
        runtime.register_scheduled_task("0 0 1 1 *", "work", "{}")

        self.assertEqual(runtime.scan_once(), 0)
        self.assertEqual(runtime.queued_count, 0)

    def test_scan_thread_survives_one_clock_failure(self) -> None:
        attempts = 0
        executed = threading.Event()

        def wall_clock() -> datetime:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("clock unavailable")
            return self.clock.wall_now()

        runtime = self.runtime(
            lambda tool_name, tool_arguments: executed.set(),
            wall_clock=wall_clock,
            scan_interval_seconds=0.01,
        )
        self.addCleanup(runtime.close)
        runtime.register_scheduled_task("* * * * *", "work", "{}")
        with self.assertLogs("myagent.scheduled_tasks", level="ERROR"):
            runtime.start()
            self.assertTrue(executed.wait(1))
        runtime.close()

        self.assertGreaterEqual(attempts, 2)

    def test_full_queue_does_not_commit_dedup_and_same_minute_retries(self) -> None:
        executed: list[str] = []
        runtime = self.runtime(lambda name, arguments: executed.append(name), max_queued_runs=1)
        runtime.register_scheduled_task("* * * * *", "first", "{}")
        runtime.register_scheduled_task("* * * * *", "second", "{}")

        self.assertEqual(runtime.scan_once(), 1)
        self.assertTrue(runtime.run_ready_once())
        self.assertEqual(runtime.scan_once(), 1)
        self.assertTrue(runtime.run_ready_once())

        self.assertEqual(executed, ["first", "second"])

    def test_jitter_is_a_not_before_time_and_orders_runs(self) -> None:
        executed: list[str] = []
        delays = deque([10.0, 1.0])
        runtime = self.runtime(
            lambda name, arguments: executed.append(name),
            max_jitter_seconds=10,
            random_uniform=lambda lower, upper: delays.popleft(),
        )
        runtime.register_scheduled_task("* * * * *", "late", "{}")
        runtime.register_scheduled_task("* * * * *", "early", "{}")
        runtime.scan_once()

        self.assertFalse(runtime.run_ready_once())
        self.clock.monotonic = 1.0
        self.assertTrue(runtime.run_ready_once())
        self.assertEqual(executed, ["early"])
        self.clock.monotonic = 9.999
        self.assertFalse(runtime.run_ready_once())
        self.clock.monotonic = 10.0
        self.assertTrue(runtime.run_ready_once())
        self.assertEqual(executed, ["early", "late"])

    def test_delete_stops_new_and_already_queued_runs(self) -> None:
        executed: list[str] = []
        runtime = self.runtime(lambda name, arguments: executed.append(name))
        registered = runtime.register_scheduled_task("* * * * *", "work", "{}")
        runtime.scan_once()

        deleted = runtime.delete_scheduled_task(registered["id"])

        self.assertEqual(deleted, {"ok": True, "deleted": True, "id": registered["id"]})
        self.assertFalse(runtime.run_ready_once())
        self.assertEqual(runtime.scan_once(), 0)
        self.assertEqual(executed, [])
        self.assertEqual(
            runtime.delete_scheduled_task(registered["id"])["code"],
            "scheduled_task_not_found",
        )

    def test_executor_exception_does_not_prevent_the_next_run(self) -> None:
        executed: list[str] = []

        def execute(tool_name: str, tool_arguments: str) -> None:
            executed.append(tool_name)
            if tool_name == "bad":
                raise RuntimeError("boom")

        runtime = self.runtime(execute)
        runtime.register_scheduled_task("* * * * *", "bad", "{}")
        runtime.register_scheduled_task("* * * * *", "good", "{}")
        runtime.scan_once()

        with self.assertLogs("myagent.scheduled_tasks", level="ERROR"):
            self.assertTrue(runtime.run_ready_once())
        self.assertTrue(runtime.run_ready_once())
        self.assertEqual(executed, ["bad", "good"])

    def test_single_consumer_serializes_executor_calls(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()
        second_finished = threading.Event()
        state_lock = threading.Lock()
        active = 0
        maximum_active = 0

        def execute(tool_name: str, tool_arguments: str) -> None:
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                if tool_name == "first":
                    first_started.set()
                    release_first.wait(1)
                else:
                    second_finished.set()
            finally:
                with state_lock:
                    active -= 1

        runtime = self.runtime(execute, scan_interval_seconds=60)
        self.addCleanup(runtime.close)
        runtime.register_scheduled_task("* * * * *", "first", "{}")
        runtime.register_scheduled_task("* * * * *", "second", "{}")
        runtime.scan_once()
        runtime.start()
        self.addCleanup(runtime.close)

        self.assertTrue(first_started.wait(1))
        self.assertFalse(second_finished.is_set())
        release_first.set()
        self.assertTrue(second_finished.wait(1))
        self.assertEqual(maximum_active, 1)

    def test_close_wakes_clears_joins_and_is_idempotent(self) -> None:
        runtime = self.runtime(
            max_jitter_seconds=30,
            random_uniform=lambda lower, upper: 30.0,
            scan_interval_seconds=60,
        )
        runtime.register_scheduled_task("* * * * *", "late", "{}")
        runtime.scan_once()
        runtime.start()
        threads = runtime.worker_threads

        runtime.close()
        runtime.close()

        self.assertEqual(len(threads), 2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(runtime.queued_count, 0)

    def test_delete_does_not_interrupt_started_run_and_close_waits_for_it(self) -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        closed = threading.Event()

        def execute(tool_name: str, tool_arguments: str) -> None:
            started.set()
            release.wait(1)
            finished.set()

        runtime = self.runtime(execute, scan_interval_seconds=60)
        registered = runtime.register_scheduled_task("* * * * *", "work", "{}")
        runtime.scan_once()
        runtime.start()
        self.assertTrue(started.wait(1))

        self.assertTrue(runtime.delete_scheduled_task(registered["id"])["deleted"])
        closer = threading.Thread(target=lambda: (runtime.close(), closed.set()))
        closer.start()
        self.assertFalse(closed.wait(0.05))
        release.set()
        self.assertTrue(finished.wait(1))
        self.assertTrue(closed.wait(1))
        closer.join()

    def test_closed_runtime_rejects_registration_and_deletion(self) -> None:
        runtime = self.runtime()
        registered = runtime.register_scheduled_task("* * * * *", "work", "{}")
        runtime.close()

        self.assertEqual(
            runtime.register_scheduled_task("* * * * *", "late", "{}")["code"],
            "scheduler_closed",
        )
        self.assertEqual(
            runtime.delete_scheduled_task(registered["id"])["code"],
            "scheduler_closed",
        )


class ScheduledTaskToolTests(unittest.TestCase):
    def test_schemas_are_strict_and_handlers_keep_structured_results(self) -> None:
        runtime = ScheduledTaskRuntime(
            lambda tool_name, tool_arguments: None,
            {"z_tool", "a_tool"},
        )
        tools = scheduled_task_tools(runtime)
        by_name = {tool.name: tool for tool in tools}

        self.assertEqual(
            set(by_name),
            {
                REGISTER_SCHEDULED_TASK_TOOL,
                REGISTER_ONE_TIME_TASK_TOOL,
                DELETE_SCHEDULED_TASK_TOOL,
            },
        )
        for tool in tools:
            self.assertTrue(tool.strict)
            self.assertFalse(tool.parameters["additionalProperties"])

        registered = by_name[REGISTER_SCHEDULED_TASK_TOOL].handler(
            cron_expression="* * * * *",
            tool_name="a_tool",
            tool_arguments='{ "value": 1 }',
        )
        self.assertTrue(registered["registered"])
        properties = by_name[REGISTER_SCHEDULED_TASK_TOOL].parameters["properties"]
        self.assertEqual(properties["tool_name"]["enum"], ["a_tool", "z_tool"])
        self.assertEqual(
            by_name[REGISTER_SCHEDULED_TASK_TOOL].parameters["required"],
            ["cron_expression", "tool_name", "tool_arguments"],
        )
        one_time = by_name[REGISTER_ONE_TIME_TASK_TOOL].handler(
            run_at="2999-01-01T00:00:00Z",
            tool_name="z_tool",
            tool_arguments="{}",
        )
        self.assertEqual(one_time["schedule_type"], ONE_TIME_SCHEDULE)
        self.assertEqual(
            by_name[REGISTER_ONE_TIME_TASK_TOOL].parameters["required"],
            ["run_at", "tool_name", "tool_arguments"],
        )
        deleted = by_name[DELETE_SCHEDULED_TASK_TOOL].handler(id=registered["id"])
        self.assertEqual(deleted["id"], registered["id"])
        self.assertTrue(deleted["deleted"])


class ScheduledTaskIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._resources: list[object] = []

    def tearDown(self) -> None:
        for resource in reversed(self._resources):
            resource.close()

    def keep(self, resource):
        self._resources.append(resource)
        return resource

    def test_default_main_tools_are_visible_strict_and_require_fresh_approval(self) -> None:
        approvals: list[str] = []
        components = self.keep(
            build_default_components(
                client=SimpleNamespace(responses=FakeResponses([])),
                approval_callback=lambda request: approvals.append(request.tool_name)
                or True,
            )
        )
        registry = components.tool_registry
        definitions = {
            definition["name"]: definition for definition in registry.definitions
        }

        self.assertTrue(SCHEDULED_TASK_TOOL_NAMES.issubset(definitions))
        for name in SCHEDULED_TASK_TOOL_NAMES:
            self.assertTrue(definitions[name]["strict"])
            self.assertFalse(
                definitions[name]["parameters"]["additionalProperties"]
            )

        registered = registry.execute(
            REGISTER_SCHEDULED_TASK_TOOL,
            '{"cron_expression":"0 0 1 1 *","tool_name":"read_file",'
            '"tool_arguments":"{\\"path\\":\\"README.md\\"}"}',
        )
        one_time = registry.execute(
            REGISTER_ONE_TIME_TASK_TOOL,
            '{"run_at":"2999-01-01T00:00:00Z","tool_name":"read_file",'
            '"tool_arguments":"{\\"path\\":\\"README.md\\"}"}',
        )
        deleted_recurring = registry.execute(
            DELETE_SCHEDULED_TASK_TOOL,
            json.dumps({"id": registered["id"]}),
        )
        deleted_one_time = registry.execute(
            DELETE_SCHEDULED_TASK_TOOL,
            json.dumps({"id": one_time["id"]}),
        )

        self.assertTrue(registered["registered"])
        self.assertTrue(one_time["registered"])
        self.assertTrue(deleted_recurring["deleted"])
        self.assertTrue(deleted_one_time["deleted"])
        self.assertEqual(
            approvals,
            [
                REGISTER_SCHEDULED_TASK_TOOL,
                REGISTER_ONE_TIME_TASK_TOOL,
                DELETE_SCHEDULED_TASK_TOOL,
                DELETE_SCHEDULED_TASK_TOOL,
            ],
        )

    def test_rejected_or_hidden_management_call_never_changes_runtime(self) -> None:
        rejected = self.keep(
            build_default_components(
                client=SimpleNamespace(responses=FakeResponses([])),
                approval_callback=lambda request: False,
            )
        )
        rejected_result = rejected.tool_registry.execute(
            REGISTER_SCHEDULED_TASK_TOOL,
            '{"cron_expression":"* * * * *","tool_name":"read_file",'
            '"tool_arguments":"{\\"path\\":\\"README.md\\"}"}',
        )
        self.assertEqual(rejected_result["permission"], "denied")
        self.assertEqual(rejected.scheduled_task_runtime.registered_count, 0)

        restricted = self.keep(
            build_default_components(
                client=SimpleNamespace(responses=FakeResponses([])),
                allowed_tools={"read_file"},
                approval_callback=lambda request: True,
            )
        )
        visible = {
            definition["name"] for definition in restricted.tool_registry.definitions
        }
        forged = restricted.tool_registry.execute(
            REGISTER_SCHEDULED_TASK_TOOL,
            '{"cron_expression":"* * * * *","tool_name":"read_file",'
            '"tool_arguments":"{\\"path\\":\\"README.md\\"}"}',
        )

        self.assertTrue(SCHEDULED_TASK_TOOL_NAMES.isdisjoint(visible))
        self.assertEqual(forged["permission"], "denied")
        self.assertEqual(restricted.scheduled_task_runtime.registered_count, 0)

    def test_agent_management_output_keeps_original_call_id(self) -> None:
        pre: list[tuple[str, str | None]] = []
        post: list[tuple[str, str | None]] = []
        hooks = HookRegistry()
        hooks.register(PreToolUse, lambda event: pre.append((event.tool_name, event.call_id)))
        hooks.register(PostToolUse, lambda event: post.append((event.tool_name, event.call_id)))
        responses = FakeResponses(
            [
                response(
                    [
                        function_call(
                            call_id="schedule-call",
                            name=REGISTER_SCHEDULED_TASK_TOOL,
                            arguments=(
                                '{"cron_expression":"0 0 1 1 *",'
                                '"tool_name":"read_file",'
                                '"tool_arguments":"{\\"path\\":\\"README.md\\"}"}'
                            ),
                        )
                    ]
                ),
                response([], "registered"),
            ]
        )
        agent = self.keep(
            AgentLoop(
                SimpleNamespace(responses=responses),
                approval_callback=lambda request: True,
                hooks=hooks,
            )
        )

        self.assertEqual(agent.run("schedule it"), "registered")

        output = next(
            item
            for item in agent.history
            if isinstance(item, dict)
            and item.get("type") == "function_call_output"
        )
        self.assertEqual(output["call_id"], "schedule-call")
        registered_id = json.loads(output["output"])["id"]
        responses._responses = iter(
            [
                response(
                    [
                        function_call(
                            call_id="delete-call",
                            name=DELETE_SCHEDULED_TASK_TOOL,
                            arguments=json.dumps({"id": registered_id}),
                        )
                    ]
                ),
                response([], "deleted"),
            ]
        )
        self.assertEqual(agent.run("delete it"), "deleted")
        self.assertTrue(
            any(
                isinstance(item, dict)
                and item.get("type") == "function_call_output"
                and item.get("call_id") == "delete-call"
                for item in agent.history
            )
        )
        self.assertEqual(
            pre,
            [
                (REGISTER_SCHEDULED_TASK_TOOL, "schedule-call"),
                (DELETE_SCHEDULED_TASK_TOOL, "delete-call"),
            ],
        )
        self.assertEqual(post, pre)

    def test_direct_scheduled_tool_crosses_hooks_without_responses_or_history(self) -> None:
        target_seen = threading.Event()
        pre: list[tuple[str, str | None]] = []
        post: list[tuple[str, str | None]] = []
        hooks = HookRegistry()
        hooks.register(PreToolUse, lambda event: pre.append((event.tool_name, event.call_id)))
        hooks.register(
            PostToolUse,
            lambda event: (
                post.append((event.tool_name, event.call_id)),
                target_seen.set() if event.tool_name == "read_file" else None,
            ),
        )
        responses = FakeResponses([])
        agent = self.keep(
            create_default_agent(
                SimpleNamespace(responses=responses),
                approval_callback=lambda request: True,
                hooks=hooks,
            )
        )
        runtime = agent.scheduled_task_runtime
        runtime._max_jitter_seconds = 0.0
        runtime._random_uniform = lambda lower, upper: 0.0
        registered = agent.tool_registry.execute(
            REGISTER_SCHEDULED_TASK_TOOL,
            '{"cron_expression":"* * * * *","tool_name":"read_file",'
            '"tool_arguments":"{\\"path\\":\\"README.md\\"}"}',
        )

        self.assertTrue(registered["ok"])
        pre.clear()
        post.clear()
        runtime.scan_once()
        self.assertTrue(target_seen.wait(1))
        self.assertEqual(pre, [("read_file", None)])
        self.assertEqual(post, [("read_file", None)])
        self.assertEqual(responses.requests, [])
        self.assertEqual(agent.history, [])

    def test_hidden_unknown_and_management_targets_are_rejected(self) -> None:
        components = self.keep(
            build_default_components(
                client=SimpleNamespace(responses=FakeResponses([])),
                allowed_tools={
                    "read_file",
                    REGISTER_SCHEDULED_TASK_TOOL,
                    DELETE_SCHEDULED_TASK_TOOL,
                },
                approval_callback=lambda request: True,
            )
        )
        runtime = components.scheduled_task_runtime
        for tool_name in (
            "write_file",
            "unknown",
            REGISTER_SCHEDULED_TASK_TOOL,
            REGISTER_ONE_TIME_TASK_TOOL,
        ):
            result = runtime.register_scheduled_task(
                "* * * * *", tool_name, "{}"
            )
            self.assertEqual(result["code"], "tool_not_schedulable")
        self.assertEqual(runtime.registered_count, 0)

    def test_reset_preserves_registrations_and_close_stops_threads_and_tools(self) -> None:
        agent = self.keep(
            create_default_agent(
                SimpleNamespace(responses=FakeResponses([])),
                approval_callback=lambda request: True,
            )
        )
        runtime = agent.scheduled_task_runtime
        registered = agent.tool_registry.execute(
            REGISTER_SCHEDULED_TASK_TOOL,
            '{"cron_expression":"0 0 1 1 *","tool_name":"read_file",'
            '"tool_arguments":"{\\"path\\":\\"README.md\\"}"}',
        )
        agent.reset()
        threads = runtime.worker_threads

        self.assertEqual(runtime.registered_count, 1)
        agent.close()

        self.assertEqual(len(threads), 2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(
            agent.tool_registry.execute(
                DELETE_SCHEDULED_TASK_TOOL,
                json.dumps({"id": registered["id"]}),
            )["code"],
            "scheduler_closed",
        )

    def test_compatibility_and_custom_registry_create_no_scheduler(self) -> None:
        before = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("myagent-scheduled-task-")
        }
        registry = build_default_tool_registry()
        custom = AgentLoop(
            SimpleNamespace(responses=FakeResponses([])),
            tool_registry=ToolRegistry(),
        )
        self.keep(custom)
        after = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("myagent-scheduled-task-")
        }

        self.assertTrue(
            SCHEDULED_TASK_TOOL_NAMES.isdisjoint(
                definition["name"] for definition in registry.definitions
            )
        )
        self.assertIsNone(custom.scheduled_task_runtime)
        self.assertEqual(after, before)

    def test_startup_failure_closes_partially_started_scheduler_threads(self) -> None:
        before = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("myagent-scheduled-task-")
        }
        real_start = ScheduledTaskRuntime.start

        def fail_after_start(runtime: ScheduledTaskRuntime) -> None:
            real_start(runtime)
            raise RuntimeError("startup failed")

        with patch.object(ScheduledTaskRuntime, "start", fail_after_start):
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                build_default_components(
                    client=SimpleNamespace(responses=FakeResponses([]))
                )

        after = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("myagent-scheduled-task-")
        }
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
