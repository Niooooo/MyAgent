"""Bounded in-process cron scheduling and strict FunctionTool adapters."""

from __future__ import annotations

import heapq
import json
import logging
import math
import random
import re
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Condition, Event, RLock, Thread, current_thread
from typing import Any

from croniter import croniter

from .tooling import FunctionTool


DEFAULT_MAX_SCHEDULED_TASKS = 100
DEFAULT_MAX_QUEUED_RUNS = 100
DEFAULT_MAX_JITTER_SECONDS = 30.0

REGISTER_SCHEDULED_TASK_TOOL = "register_scheduled_task"
REGISTER_ONE_TIME_TASK_TOOL = "register_one_time_task"
DELETE_SCHEDULED_TASK_TOOL = "delete_scheduled_task"
SCHEDULED_TASK_TOOL_NAMES = frozenset(
    {
        REGISTER_SCHEDULED_TASK_TOOL,
        REGISTER_ONE_TIME_TASK_TOOL,
        DELETE_SCHEDULED_TASK_TOOL,
    }
)

RECURRING_SCHEDULE = "recurring"
ONE_TIME_SCHEDULE = "one_time"

_SCHEDULED_TASK_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SCAN_INTERVAL_SECONDS = 1.0
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScheduledTask:
    """One immutable recurring or one-time in-process registration."""

    id: str
    schedule_type: str
    cron_expression: str | None
    run_at: datetime | None
    tool_name: str
    tool_arguments: str


@dataclass(order=True, frozen=True, slots=True)
class _QueuedRun:
    """One delayed execution ordered by monotonic readiness and insertion order."""

    ready_at: float
    sequence: int
    scheduled_task_id: str = field(compare=False)
    deduplication_key: datetime = field(compare=False)
    task_snapshot: ScheduledTask = field(compare=False)


class ScheduledTaskRuntime:
    """Own a bounded cron registry, delayed queue, and two worker threads."""

    def __init__(
        self,
        task_executor: Callable[[str, str], object],
        schedulable_tool_names: Iterable[str],
        *,
        max_scheduled_tasks: int = DEFAULT_MAX_SCHEDULED_TASKS,
        max_queued_runs: int = DEFAULT_MAX_QUEUED_RUNS,
        max_jitter_seconds: float = DEFAULT_MAX_JITTER_SECONDS,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        random_uniform: Callable[[float, float], float] | None = None,
        scan_interval_seconds: float = _SCAN_INTERVAL_SECONDS,
    ) -> None:
        if not callable(task_executor):
            raise TypeError("task_executor must be callable")
        if isinstance(schedulable_tool_names, str):
            raise TypeError("schedulable_tool_names must be an iterable of names")
        try:
            normalized_tool_names = frozenset(schedulable_tool_names)
        except TypeError as exc:
            raise TypeError("schedulable_tool_names must be an iterable of names") from exc
        if any(not isinstance(name, str) or not name for name in normalized_tool_names):
            raise ValueError("schedulable_tool_names must contain non-empty strings")
        normalized_tool_names = normalized_tool_names.difference(
            SCHEDULED_TASK_TOOL_NAMES
        )
        _require_positive_integer(max_scheduled_tasks, "max_scheduled_tasks")
        _require_positive_integer(max_queued_runs, "max_queued_runs")
        _require_nonnegative_finite(max_jitter_seconds, "max_jitter_seconds")
        _require_positive_finite(scan_interval_seconds, "scan_interval_seconds")
        if wall_clock is not None and not callable(wall_clock):
            raise TypeError("wall_clock must be callable")
        if monotonic_clock is not None and not callable(monotonic_clock):
            raise TypeError("monotonic_clock must be callable")
        if random_uniform is not None and not callable(random_uniform):
            raise TypeError("random_uniform must be callable")

        self._task_executor = task_executor
        self._schedulable_tool_names = normalized_tool_names
        self._max_scheduled_tasks = max_scheduled_tasks
        self._max_queued_runs = max_queued_runs
        self._max_jitter_seconds = float(max_jitter_seconds)
        self._wall_clock = wall_clock or _local_now
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._random_uniform = random_uniform or random.uniform
        self._scan_interval_seconds = float(scan_interval_seconds)

        self._lock = RLock()
        self._queue_changed = Condition(self._lock)
        self._stop_event = Event()
        self._tasks: dict[str, ScheduledTask] = {}
        # Recurring tasks retain their latest minute; one-time tasks retain run_at.
        self._last_enqueued_at: dict[str, datetime] = {}
        self._queue: list[_QueuedRun] = []
        self._next_sequence = 0
        self._started = False
        self._closed = False
        self._scanner_thread: Thread | None = None
        self._consumer_thread: Thread | None = None

    @property
    def registered_count(self) -> int:
        with self._lock:
            return len(self._tasks)

    @property
    def queued_count(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def worker_threads(self) -> tuple[Thread, ...]:
        """Return created workers for lifecycle inspection without owning them."""
        with self._lock:
            return tuple(
                thread
                for thread in (self._scanner_thread, self._consumer_thread)
                if thread is not None
            )

    @property
    def schedulable_tool_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._schedulable_tool_names))

    def start(self) -> None:
        """Idempotently start the scanner and single serial consumer."""
        started: list[Thread] = []
        try:
            # Keep start and close mutually exclusive until both Thread.start calls
            # return. The workers may begin immediately, but block on this lock.
            with self._queue_changed:
                if self._started or self._closed:
                    return
                self._started = True
                scanner = Thread(
                    target=self._scanner_loop,
                    name="myagent-scheduled-task-scanner",
                    daemon=False,
                )
                consumer = Thread(
                    target=self._consumer_loop,
                    name="myagent-scheduled-task-consumer",
                    daemon=False,
                )
                self._scanner_thread = scanner
                self._consumer_thread = consumer
                scanner.start()
                started.append(scanner)
                consumer.start()
                started.append(consumer)
        except BaseException:
            with self._queue_changed:
                self._closed = True
                self._queue.clear()
                self._stop_event.set()
                self._queue_changed.notify_all()
            for thread in started:
                thread.join()
            raise

    def close(self) -> None:
        """Reject mutations, abandon pending runs, wake workers, and join them."""
        with self._queue_changed:
            if not self._closed:
                self._closed = True
                self._queue.clear()
                self._stop_event.set()
                self._queue_changed.notify_all()
            threads = tuple(
                thread
                for thread in (self._scanner_thread, self._consumer_thread)
                if thread is not None
            )

        caller = current_thread()
        for thread in threads:
            if thread is not caller:
                thread.join()

    def register_scheduled_task(
        self,
        cron_expression: object,
        tool_name: object,
        tool_arguments: object,
    ) -> dict[str, Any]:
        """Validate and atomically register one in-process scheduled task."""
        with self._lock:
            if self._closed:
                return _error("scheduler_closed", "Scheduled task runtime is closed")

        normalized_cron = _normalize_cron_expression(cron_expression)
        if normalized_cron is None:
            return _error(
                "invalid_cron_expression",
                "cron_expression must be a valid standard five-field cron expression",
            )
        target = self._normalize_target(tool_name, tool_arguments)
        if isinstance(target, dict):
            return target
        normalized_tool_name, normalized_arguments = target
        registered = self._register_normalized(
            schedule_type=RECURRING_SCHEDULE,
            cron_expression=normalized_cron,
            run_at=None,
            tool_name=normalized_tool_name,
            tool_arguments=normalized_arguments,
        )
        if isinstance(registered, dict):
            return registered
        scheduled = registered

        return {
            "ok": True,
            "registered": True,
            "id": scheduled.id,
            "schedule_type": scheduled.schedule_type,
            "cron_expression": scheduled.cron_expression,
            "tool_name": scheduled.tool_name,
            "tool_arguments": scheduled.tool_arguments,
        }

    def register_one_time_task(
        self,
        run_at: object,
        tool_name: object,
        tool_arguments: object,
    ) -> dict[str, Any]:
        """Register one direct tool call for one future instant."""
        with self._lock:
            if self._closed:
                return _error("scheduler_closed", "Scheduled task runtime is closed")
        try:
            wall_now = _as_aware_datetime(self._wall_clock())
        except Exception:
            return _error(
                "scheduler_clock_unavailable",
                "Scheduled task runtime clock is unavailable",
            )
        normalized_run_at = _normalize_run_at(run_at)
        if normalized_run_at is None:
            return _error(
                "invalid_run_at",
                "run_at must be an ISO 8601 datetime with a timezone offset",
            )
        if normalized_run_at <= wall_now.astimezone(timezone.utc):
            return _error("run_at_not_future", "run_at must be in the future")
        target = self._normalize_target(tool_name, tool_arguments)
        if isinstance(target, dict):
            return target
        normalized_tool_name, normalized_arguments = target
        registered = self._register_normalized(
            schedule_type=ONE_TIME_SCHEDULE,
            cron_expression=None,
            run_at=normalized_run_at,
            tool_name=normalized_tool_name,
            tool_arguments=normalized_arguments,
        )
        if isinstance(registered, dict):
            return registered
        scheduled = registered
        return {
            "ok": True,
            "registered": True,
            "id": scheduled.id,
            "schedule_type": scheduled.schedule_type,
            "run_at": _format_run_at(scheduled.run_at),
            "tool_name": scheduled.tool_name,
            "tool_arguments": scheduled.tool_arguments,
        }

    def delete_scheduled_task(self, scheduled_task_id: object) -> dict[str, Any]:
        """Atomically delete a registration and its bounded deduplication state."""
        with self._lock:
            if self._closed:
                return _error("scheduler_closed", "Scheduled task runtime is closed")
            if (
                not isinstance(scheduled_task_id, str)
                or not _SCHEDULED_TASK_ID_PATTERN.fullmatch(scheduled_task_id)
            ):
                return _error(
                    "invalid_scheduled_task_id",
                    "id must be exactly 32 lowercase hexadecimal characters",
                )
            if self._tasks.pop(scheduled_task_id, None) is None:
                return _error(
                    "scheduled_task_not_found",
                    "Scheduled task does not exist",
                )
            self._last_enqueued_at.pop(scheduled_task_id, None)

        return {"ok": True, "deleted": True, "id": scheduled_task_id}

    def scan_once(self) -> int:
        """Perform one non-blocking scan; exposed as a deterministic runtime seam."""
        with self._lock:
            if self._closed:
                return 0
        wall_now = _as_aware_datetime(self._wall_clock())
        minute_bucket = _as_local_minute(wall_now)
        with self._lock:
            if self._closed:
                return 0
            snapshot = tuple(self._tasks.values())

        enqueued = 0
        for scheduled in snapshot:
            if scheduled.schedule_type == ONE_TIME_SCHEDULE:
                matches = scheduled.run_at is not None and (
                    wall_now.astimezone(timezone.utc) >= scheduled.run_at
                )
                deduplication_key = scheduled.run_at
            else:
                try:
                    matches = croniter.match(
                        scheduled.cron_expression,
                        minute_bucket,
                    )
                except Exception:
                    _LOGGER.exception(
                        "Cron matching failed for scheduled task %s",
                        scheduled.id,
                    )
                    continue
                deduplication_key = minute_bucket
            if not matches:
                continue
            if self._enqueue_match(scheduled, deduplication_key):
                enqueued += 1
        return enqueued

    def run_ready_once(self) -> bool:
        """Execute one ready run before ``start()`` as a deterministic test seam."""
        with self._queue_changed:
            if self._started:
                raise RuntimeError("run_ready_once is only available before start")
            if self._closed or not self._queue:
                return False
            if self._queue[0].ready_at > self._monotonic_clock():
                return False
            queued = heapq.heappop(self._queue)
        return self._execute_if_current(queued)

    def _enqueue_match(
        self,
        scheduled: ScheduledTask,
        deduplication_key: datetime,
    ) -> bool:
        with self._lock:
            if (
                self._closed
                or self._tasks.get(scheduled.id) != scheduled
                or self._last_enqueued_at.get(scheduled.id) == deduplication_key
                or len(self._queue) >= self._max_queued_runs
            ):
                return False

        try:
            delay = float(self._random_uniform(0.0, self._max_jitter_seconds))
            monotonic_now = float(self._monotonic_clock())
        except Exception:
            _LOGGER.exception(
                "Jitter calculation failed for scheduled task %s",
                scheduled.id,
            )
            return False
        if not math.isfinite(delay) or not 0.0 <= delay <= self._max_jitter_seconds:
            _LOGGER.error(
                "Jitter source returned an out-of-range delay for scheduled task %s",
                scheduled.id,
            )
            return False
        if not math.isfinite(monotonic_now):
            _LOGGER.error("Monotonic clock returned a non-finite value")
            return False

        with self._queue_changed:
            if (
                self._closed
                or self._tasks.get(scheduled.id) != scheduled
                or self._last_enqueued_at.get(scheduled.id) == deduplication_key
                or len(self._queue) >= self._max_queued_runs
            ):
                return False
            queued = _QueuedRun(
                ready_at=monotonic_now + delay,
                sequence=self._next_sequence,
                scheduled_task_id=scheduled.id,
                deduplication_key=deduplication_key,
                task_snapshot=scheduled,
            )
            self._next_sequence += 1
            heapq.heappush(self._queue, queued)
            # Queue insertion and minute deduplication commit are one transaction.
            self._last_enqueued_at[scheduled.id] = deduplication_key
            self._queue_changed.notify()
            return True

    def _scanner_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.scan_once()
            except Exception:
                _LOGGER.exception("Scheduled task scan failed")
            if self._stop_event.wait(self._scan_interval_seconds):
                return

    def _consumer_loop(self) -> None:
        while True:
            try:
                queued = self._wait_for_ready_run()
                if queued is None:
                    return
                self._execute_if_current(queued)
            except Exception:
                _LOGGER.exception("Scheduled task consumer iteration failed")

    def _wait_for_ready_run(self) -> _QueuedRun | None:
        with self._queue_changed:
            while True:
                if self._closed:
                    return None
                if not self._queue:
                    self._queue_changed.wait()
                    continue
                delay = self._queue[0].ready_at - self._monotonic_clock()
                if delay > 0:
                    self._queue_changed.wait(timeout=delay)
                    continue
                return heapq.heappop(self._queue)

    def _execute_if_current(self, queued: _QueuedRun) -> bool:
        # Acquiring the registry lock defines the execution-start boundary. A delete
        # that wins this race makes the queued snapshot stale; a later delete does
        # not interrupt work that has already started.
        with self._lock:
            if (
                self._closed
                or self._tasks.get(queued.scheduled_task_id)
                != queued.task_snapshot
            ):
                return False
            tool_name = queued.task_snapshot.tool_name
            tool_arguments = queued.task_snapshot.tool_arguments
            if queued.task_snapshot.schedule_type == ONE_TIME_SCHEDULE:
                self._tasks.pop(queued.scheduled_task_id, None)
                self._last_enqueued_at.pop(queued.scheduled_task_id, None)
        try:
            self._task_executor(tool_name, tool_arguments)
        except Exception:
            _LOGGER.exception(
                "Scheduled task execution failed for %s",
                queued.scheduled_task_id,
            )
        return True

    def _normalize_target(
        self,
        tool_name: object,
        tool_arguments: object,
    ) -> tuple[str, str] | dict[str, Any]:
        if not isinstance(tool_name, str) or not tool_name.strip():
            return _error("invalid_tool_name", "tool_name must be a non-empty string")
        normalized_tool_name = tool_name.strip()
        if normalized_tool_name not in self._schedulable_tool_names:
            return _error(
                "tool_not_schedulable",
                f"tool is not available for scheduling: {normalized_tool_name}",
            )
        normalized_arguments = _normalize_tool_arguments(tool_arguments)
        if normalized_arguments is None:
            return _error(
                "invalid_tool_arguments",
                "tool_arguments must be a JSON object encoded as a string",
            )
        return normalized_tool_name, normalized_arguments

    def _register_normalized(
        self,
        *,
        schedule_type: str,
        cron_expression: str | None,
        run_at: datetime | None,
        tool_name: str,
        tool_arguments: str,
    ) -> ScheduledTask | dict[str, Any]:
        with self._lock:
            if self._closed:
                return _error("scheduler_closed", "Scheduled task runtime is closed")
            if len(self._tasks) >= self._max_scheduled_tasks:
                return _error(
                    "scheduled_task_limit_reached",
                    f"at most {self._max_scheduled_tasks} scheduled tasks are allowed",
                )
            scheduled = ScheduledTask(
                id=self._new_task_id_locked(),
                schedule_type=schedule_type,
                cron_expression=cron_expression,
                run_at=run_at,
                tool_name=tool_name,
                tool_arguments=tool_arguments,
            )
            self._tasks[scheduled.id] = scheduled
            return scheduled

    def _new_task_id_locked(self) -> str:
        while True:
            candidate = secrets.token_hex(16)
            if candidate not in self._tasks:
                return candidate


def scheduled_task_tools(runtime: ScheduledTaskRuntime) -> list[FunctionTool]:
    """Adapt one runtime to the three strict main-agent management tools."""
    if not isinstance(runtime, ScheduledTaskRuntime):
        raise TypeError("runtime must be a ScheduledTaskRuntime")

    def register_scheduled_task(
        cron_expression: str,
        tool_name: str,
        tool_arguments: str,
    ) -> dict[str, Any]:
        return runtime.register_scheduled_task(
            cron_expression,
            tool_name,
            tool_arguments,
        )

    def delete_scheduled_task(id: str) -> dict[str, Any]:
        return runtime.delete_scheduled_task(id)

    def register_one_time_task(
        run_at: str,
        tool_name: str,
        tool_arguments: str,
    ) -> dict[str, Any]:
        return runtime.register_one_time_task(run_at, tool_name, tool_arguments)

    return [
        FunctionTool(
            name=REGISTER_SCHEDULED_TASK_TOOL,
            description=(
                "Register a direct tool call on a standard five-field local-time "
                "cron schedule in this process. Registration requires user approval."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "cron_expression": {
                        "type": "string",
                        "description": (
                            "Standard five-field cron: minute hour day-of-month "
                            "month day-of-week."
                        ),
                    },
                    "tool_name": {
                        "type": "string",
                        "enum": list(runtime.schedulable_tool_names),
                        "description": "Existing visible tool to invoke when cron matches.",
                    },
                    "tool_arguments": {
                        "type": "string",
                        "description": (
                            "Target tool arguments encoded as a JSON object string."
                        ),
                    },
                },
                "required": ["cron_expression", "tool_name", "tool_arguments"],
                "additionalProperties": False,
            },
            handler=register_scheduled_task,
            strict=True,
        ),
        FunctionTool(
            name=REGISTER_ONE_TIME_TASK_TOOL,
            description=(
                "Register one direct tool call for a future ISO 8601 datetime. "
                "The datetime must include a timezone offset. Registration "
                "requires user approval."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "run_at": {
                        "type": "string",
                        "description": (
                            "Future ISO 8601 datetime including timezone, for example "
                            "2026-08-05T09:30:00+08:00."
                        ),
                    },
                    "tool_name": {
                        "type": "string",
                        "enum": list(runtime.schedulable_tool_names),
                        "description": "Existing visible tool to invoke once.",
                    },
                    "tool_arguments": {
                        "type": "string",
                        "description": (
                            "Target tool arguments encoded as a JSON object string."
                        ),
                    },
                },
                "required": ["run_at", "tool_name", "tool_arguments"],
                "additionalProperties": False,
            },
            handler=register_one_time_task,
            strict=True,
        ),
        FunctionTool(
            name=DELETE_SCHEDULED_TASK_TOOL,
            description=(
                "Delete one in-process scheduled task. Pending runs for that "
                "registration will be skipped. Deletion requires user approval."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "pattern": _SCHEDULED_TASK_ID_PATTERN.pattern,
                        "minLength": 32,
                        "maxLength": 32,
                    }
                },
                "required": ["id"],
                "additionalProperties": False,
            },
            handler=delete_scheduled_task,
            strict=True,
        ),
    ]


def _normalize_cron_expression(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    fields = value.split()
    if len(fields) != 5:
        return None
    normalized = " ".join(fields)
    try:
        if not croniter.is_valid(normalized, strict=True):
            return None
    except (TypeError, ValueError, KeyError):
        return None
    return normalized


def _normalize_tool_arguments(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_run_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _format_run_at(value: datetime | None) -> str:
    if value is None:
        raise ValueError("one-time task is missing run_at")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _reject_json_constant(constant: str) -> None:
    raise ValueError(f"invalid JSON constant: {constant}")


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _as_aware_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("wall_clock must return a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("wall_clock must return a timezone-aware datetime")
    return value


def _as_local_minute(value: datetime) -> datetime:
    return _as_aware_datetime(value).astimezone().replace(second=0, microsecond=0)


def _error(code: str, error: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": error}


def _require_positive_integer(value: object, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be positive")


def _require_nonnegative_finite(value: object, field_name: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be numeric")
    if not math.isfinite(float(value)) or float(value) < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")


def _require_positive_finite(value: object, field_name: str) -> None:
    _require_nonnegative_finite(value, field_name)
    if float(value) <= 0:
        raise ValueError(f"{field_name} must be positive")
