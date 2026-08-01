"""Isolated synchronous and background child-agent execution."""

from __future__ import annotations

import secrets
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, Any

from .tooling import FunctionTool

if TYPE_CHECKING:
    from .agent import AgentLoop


RUN_SUBAGENT_TOOL = "run_subagent"
FORK_SUBAGENT_TOOL = "fork_subagent"
COLLECT_SUBAGENT_TOOL = "collect_subagent"
SUBAGENT_TOOL_NAMES = frozenset(
    {
        RUN_SUBAGENT_TOOL,
        FORK_SUBAGENT_TOOL,
        COLLECT_SUBAGENT_TOOL,
    }
)

DEFAULT_SUBAGENT_MAX_WORKERS = 4
DEFAULT_SUBAGENT_MAX_TASKS = 16
MAX_COLLECT_TIMEOUT_SECONDS = 300.0
_MAX_ERROR_CHARS = 2_000

AgentFactory = Callable[[], "AgentLoop"]


@dataclass
class _ForkRecord:
    future: Future[str] | None
    status: str = "running"
    output: str | None = None
    error: str | None = None


class SubAgentManager:
    """Run fresh child agents and retain only bounded result state."""

    def __init__(
        self,
        agent_factory: AgentFactory,
        *,
        max_workers: int = DEFAULT_SUBAGENT_MAX_WORKERS,
        max_tasks: int = DEFAULT_SUBAGENT_MAX_TASKS,
    ) -> None:
        if not callable(agent_factory):
            raise TypeError("agent_factory must be callable")
        if not isinstance(max_workers, int) or isinstance(max_workers, bool):
            raise TypeError("max_workers must be an integer")
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if not isinstance(max_tasks, int) or isinstance(max_tasks, bool):
            raise TypeError("max_tasks must be an integer")
        if max_tasks <= 0:
            raise ValueError("max_tasks must be positive")

        self._agent_factory = agent_factory
        self._max_tasks = max_tasks
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="myagent-subagent",
        )
        self._lock = RLock()
        self._records: dict[str, _ForkRecord] = {}
        self._cleaned_order: deque[str] = deque()
        self._cleaned_ids: set[str] = set()
        self._closed = False

    def run_subagent(self, task: str) -> dict[str, Any]:
        """Run one isolated child agent synchronously."""
        invalid = _validate_task(task)
        if invalid is not None:
            return invalid
        with self._lock:
            if self._closed:
                return _manager_closed_result()
            future = self._executor.submit(self._run_fresh_agent, task.strip())

        try:
            output = future.result()
        except Exception as exc:
            return {
                "ok": False,
                "status": "failed",
                "code": "subagent_failed",
                "error": _error_summary(exc),
            }
        return {"ok": True, "output": output}

    def fork_subagent(self, task: str) -> dict[str, Any]:
        """Submit one isolated child agent to the bounded background pool."""
        invalid = _validate_task(task)
        if invalid is not None:
            return invalid

        with self._lock:
            if self._closed:
                return _manager_closed_result()
            if len(self._records) >= self._max_tasks:
                return {
                    "ok": False,
                    "status": "rejected",
                    "code": "task_limit_reached",
                    "error": (
                        "Sub-agent task limit reached; collect completed tasks "
                        "before forking another"
                    ),
                }

            fork_id = self._new_fork_id()
            future = self._executor.submit(self._run_fresh_agent, task.strip())
            self._records[fork_id] = _ForkRecord(future=future)
            future.add_done_callback(
                lambda completed, identifier=fork_id: self._finish(
                    identifier,
                    completed,
                )
            )

        return {
            "ok": True,
            "fork_id": fork_id,
            "status": "running",
        }

    def collect_subagent(
        self,
        fork_id: str,
        wait: bool = False,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Poll or consume one fork result, waiting for a bounded time if asked."""
        invalid = _validate_collect_arguments(fork_id, wait, timeout_seconds)
        if invalid is not None:
            return invalid

        normalized_id = fork_id.strip()
        with self._lock:
            record = self._records.get(normalized_id)
            if record is None:
                return self._missing_result(normalized_id)
            future = record.future
            status = record.status

        if status == "running" and future is None:
            raise RuntimeError("running sub-agent record is missing its Future")

        if status == "running" and wait:
            try:
                future.result(timeout=float(timeout_seconds))
            except TimeoutError:
                return {
                    "ok": False,
                    "fork_id": normalized_id,
                    "status": "running",
                    "code": "timeout",
                    "error": (
                        "Timed out while waiting for the sub-agent; "
                        "the task is still running"
                    ),
                }
            except BaseException:
                # _finish converts worker failures into a bounded summary below.
                pass
            self._finish(normalized_id, future)

        with self._lock:
            record = self._records.get(normalized_id)
            if record is None:
                return self._missing_result(normalized_id)
            if record.status == "running":
                return {
                    "ok": True,
                    "fork_id": normalized_id,
                    "status": "running",
                }

            self._records.pop(normalized_id)
            self._remember_cleaned(normalized_id)
            if record.status == "completed":
                return {
                    "ok": True,
                    "fork_id": normalized_id,
                    "status": "completed",
                    "output": record.output,
                }
            return {
                "ok": False,
                "fork_id": normalized_id,
                "status": "failed",
                "code": "subagent_failed",
                "error": record.error or "Sub-agent failed",
            }

    def close(self) -> None:
        """Reject new work and release all executor and result state."""
        with self._lock:
            if self._closed:
                return
            self._closed = True

        self._executor.shutdown(wait=True, cancel_futures=True)

        with self._lock:
            for fork_id in tuple(self._records):
                self._remember_cleaned(fork_id)
            self._records.clear()

    def _run_fresh_agent(self, task: str) -> str:
        agent = self._agent_factory()
        try:
            return agent.run(task)
        finally:
            agent.close()

    def _finish(self, fork_id: str, future: Future[str]) -> None:
        try:
            output = future.result()
        except BaseException as exc:
            status = "failed"
            output = None
            error = _error_summary(exc)
        else:
            status = "completed"
            error = None

        with self._lock:
            record = self._records.get(fork_id)
            if record is None or record.status != "running":
                return
            record.status = status
            record.output = output
            record.error = error
            record.future = None

    def _new_fork_id(self) -> str:
        while True:
            candidate = secrets.token_urlsafe(18)
            if candidate not in self._records and candidate not in self._cleaned_ids:
                return candidate

    def _remember_cleaned(self, fork_id: str) -> None:
        cleaned_limit = self._max_tasks * 4
        while len(self._cleaned_order) >= cleaned_limit:
            expired = self._cleaned_order.popleft()
            self._cleaned_ids.discard(expired)
        self._cleaned_order.append(fork_id)
        self._cleaned_ids.add(fork_id)

    def _missing_result(self, fork_id: str) -> dict[str, Any]:
        if fork_id in self._cleaned_ids:
            return {
                "ok": False,
                "fork_id": fork_id,
                "status": "cleaned",
                "code": "fork_cleaned",
                "error": "Sub-agent result has already been collected or cleaned",
            }
        return {
            "ok": False,
            "fork_id": fork_id,
            "status": "unknown",
            "code": "unknown_fork_id",
            "error": "Unknown sub-agent fork_id",
        }


def subagent_tools(manager: SubAgentManager) -> list[FunctionTool]:
    """Build the complete management-tool family for a main agent only."""
    return [
        FunctionTool(
            name=RUN_SUBAGENT_TOOL,
            description=(
                "Run a fresh isolated sub-agent synchronously and return only its "
                "final text. The sub-agent cannot delegate to other sub-agents."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The complete task for the isolated sub-agent.",
                    }
                },
                "required": ["task"],
                "additionalProperties": False,
            },
            handler=manager.run_subagent,
            strict=True,
        ),
        FunctionTool(
            name=FORK_SUBAGENT_TOOL,
            description=(
                "Start a fresh isolated sub-agent in the bounded background pool "
                "and immediately return a fork_id."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The complete background task.",
                    }
                },
                "required": ["task"],
                "additionalProperties": False,
            },
            handler=manager.fork_subagent,
            strict=True,
        ),
        FunctionTool(
            name=COLLECT_SUBAGENT_TOOL,
            description=(
                "Poll or collect a forked sub-agent. Completed results are returned "
                "once and contain only final text."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "fork_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Identifier returned by fork_subagent.",
                    },
                    "wait": {
                        "type": "boolean",
                        "default": False,
                        "description": "Wait briefly instead of polling immediately.",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": MAX_COLLECT_TIMEOUT_SECONDS,
                        "default": 30,
                        "description": "Maximum wait when wait is true.",
                    },
                },
                "required": ["fork_id"],
                "additionalProperties": False,
            },
            handler=manager.collect_subagent,
            strict=True,
        ),
    ]


def _validate_task(task: object) -> dict[str, Any] | None:
    if not isinstance(task, str) or not task.strip():
        return {
            "ok": False,
            "status": "rejected",
            "code": "invalid_task",
            "error": "task must be a non-empty string",
        }
    return None


def _validate_collect_arguments(
    fork_id: object,
    wait: object,
    timeout_seconds: object,
) -> dict[str, Any] | None:
    if not isinstance(fork_id, str) or not fork_id.strip():
        return {
            "ok": False,
            "status": "rejected",
            "code": "invalid_fork_id",
            "error": "fork_id must be a non-empty string",
        }
    if not isinstance(wait, bool):
        return {
            "ok": False,
            "fork_id": fork_id.strip(),
            "status": "rejected",
            "code": "invalid_wait",
            "error": "wait must be a boolean",
        }
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not 0 <= float(timeout_seconds) <= MAX_COLLECT_TIMEOUT_SECONDS
    ):
        return {
            "ok": False,
            "fork_id": fork_id.strip(),
            "status": "rejected",
            "code": "invalid_timeout",
            "error": (
                "timeout_seconds must be between 0 and "
                f"{MAX_COLLECT_TIMEOUT_SECONDS:g}"
            ),
        }
    return None


def _manager_closed_result() -> dict[str, Any]:
    return {
        "ok": False,
        "status": "closed",
        "code": "manager_closed",
        "error": "Sub-agent manager is closed",
    }


def _error_summary(exc: BaseException) -> str:
    summary = f"{type(exc).__name__}: {exc}"
    if len(summary) <= _MAX_ERROR_CHARS:
        return summary
    return summary[:_MAX_ERROR_CHARS] + "..."
