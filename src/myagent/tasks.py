"""Workspace-persistent DAG tasks and their FunctionTool adapters."""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any

from .tooling import FunctionTool


TASK_SCHEMA_VERSION = 1
TASK_DIRECTORY = Path(".myagent") / "tasks"
TASK_FILE_NAME = "tasks.json"

MAX_TASKS = 500
MAX_TASK_DEPENDENCIES = 50
MAX_TASK_NAME_CHARS = 200
MAX_TASK_SUMMARY_CHARS = 1_000
MAX_TASK_TOPIC_CHARS = 200
MAX_TASK_OWNER_CHARS = 200
MAX_TASK_FILE_BYTES = 1_048_576

CREATE_TASK_TOOL = "create_task"
IS_TASK_EXECUTABLE_TOOL = "is_task_executable"
CLAIM_TASK_TOOL = "claim_task"
COMPLETE_TASK_TOOL = "complete_task"
LIST_TASKS_TOOL = "list_tasks"
GET_TASK_TOOL = "get_task"
TASK_TOOL_NAMES = frozenset(
    {
        CREATE_TASK_TOOL,
        IS_TASK_EXECUTABLE_TOOL,
        CLAIM_TASK_TOOL,
        COMPLETE_TASK_TOOL,
        LIST_TASKS_TOOL,
        GET_TASK_TOOL,
    }
)
TASK_MUTATION_TOOL_NAMES = frozenset(
    {CREATE_TASK_TOOL, CLAIM_TASK_TOOL, COMPLETE_TASK_TOOL}
)

_TASK_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_TASK_KEYS = frozenset(
    {"id", "name", "status", "summary", "topic", "owner", "dependencies"}
)
_DOCUMENT_KEYS = frozenset({"schema_version", "tasks"})


class TaskStatus(str, Enum):
    """The complete lifecycle of one persistent task."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class Task:
    """One immutable task in the workspace DAG."""

    id: str
    name: str
    status: TaskStatus
    summary: str
    topic: str
    owner: str | None
    dependencies: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status.value,
            "summary": self.summary,
            "topic": self.topic,
            "owner": self.owner,
            "dependencies": list(self.dependencies),
        }


@dataclass(frozen=True, slots=True)
class _Executability:
    executable: bool
    reason: str
    blocking_dependency_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "executable": self.executable,
            "reason": self.reason,
            "blocking_dependency_ids": list(self.blocking_dependency_ids),
        }


class TaskStoreError(RuntimeError):
    """Expected task validation or storage failure safe for tool output."""

    def __init__(
        self,
        code: str,
        error: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(error)
        self.code = code
        self.error = error
        self.details = dict(details or {})


class TaskStore:
    """Own, validate, and atomically persist one workspace task graph.

    All operations reread the file while holding this instance's shared ``RLock``.
    The lock serializes parent/child Agent threads that share this Store; it is not
    a multi-process or distributed lock.
    """

    def __init__(
        self,
        workspace_root: str | os.PathLike[str] | None = None,
        *,
        _id_factory: Callable[[], str] | None = None,
    ) -> None:
        if _id_factory is not None and not callable(_id_factory):
            raise TypeError("_id_factory must be callable")
        self.workspace_root = Path(workspace_root or Path.cwd()).resolve()
        self.root = self.workspace_root / TASK_DIRECTORY
        self.path = self.root / TASK_FILE_NAME
        self._id_factory = _id_factory or (lambda: secrets.token_hex(16))
        self._lock = RLock()

    def create_task(
        self,
        name: object,
        summary: object,
        topic: object,
        dependencies: object,
    ) -> dict[str, Any]:
        """Create one pending task whose dependencies already exist."""
        with self._lock:
            try:
                normalized_name = self._validate_input_string(
                    name,
                    field="name",
                    maximum=MAX_TASK_NAME_CHARS,
                    code="invalid_task_name",
                )
                normalized_summary = self._validate_input_string(
                    summary,
                    field="summary",
                    maximum=MAX_TASK_SUMMARY_CHARS,
                    code="invalid_task_summary",
                )
                normalized_topic = self._validate_input_string(
                    topic,
                    field="topic",
                    maximum=MAX_TASK_TOPIC_CHARS,
                    code="invalid_task_topic",
                )
                normalized_dependencies = self._validate_input_dependencies(
                    dependencies
                )
                current = self._read_tasks()
                if len(current) >= MAX_TASKS:
                    raise TaskStoreError(
                        "task_limit_reached",
                        f"Task limit of {MAX_TASKS} has been reached",
                    )
                current_by_id = {task.id: task for task in current}
                missing = tuple(
                    dependency
                    for dependency in normalized_dependencies
                    if dependency not in current_by_id
                )
                if missing:
                    raise TaskStoreError(
                        "invalid_task_dependencies",
                        "dependencies must reference existing tasks",
                        details={"missing_dependency_ids": list(missing)},
                    )

                task = Task(
                    id=self._new_task_id(current_by_id),
                    name=normalized_name,
                    status=TaskStatus.PENDING,
                    summary=normalized_summary,
                    topic=normalized_topic,
                    owner=None,
                    dependencies=normalized_dependencies,
                )
                proposal = tuple((*current, task))
                self._commit(current, proposal)
                return {"changed": True, **self._task_details(task, proposal)}
            except TaskStoreError:
                raise
            except OSError as exc:
                raise self._storage_error() from exc

    def is_task_executable(self, id: object) -> dict[str, Any]:
        """Return whether a task is currently ready to be claimed."""
        with self._lock:
            try:
                task_id = self._validate_input_id(id)
                tasks = self._read_tasks()
                task = self._require_task(tasks, task_id)
                return {"id": task.id, **self._executability(task, tasks).as_dict()}
            except TaskStoreError:
                raise
            except OSError as exc:
                raise self._storage_error() from exc

    def claim_task(self, id: object, owner: object) -> dict[str, Any]:
        """Atomically claim a currently executable task for an explicit owner."""
        with self._lock:
            try:
                task_id = self._validate_input_id(id)
                normalized_owner = self._validate_input_string(
                    owner,
                    field="owner",
                    maximum=MAX_TASK_OWNER_CHARS,
                    code="invalid_task_owner",
                )
                current = self._read_tasks()
                task = self._require_task(current, task_id)

                if task.status is TaskStatus.IN_PROGRESS:
                    if task.owner == normalized_owner:
                        return {
                            "changed": False,
                            **self._task_details(task, current),
                        }
                    raise TaskStoreError(
                        "task_already_claimed",
                        "Task is already claimed by another owner",
                    )
                if task.status is TaskStatus.COMPLETED:
                    raise TaskStoreError(
                        "invalid_task_transition",
                        "A completed task cannot be claimed",
                    )

                executability = self._executability(task, current)
                if not executability.executable:
                    raise TaskStoreError(
                        "task_not_executable",
                        "Task is not currently executable",
                        details=executability.as_dict(),
                    )

                claimed = replace(
                    task,
                    status=TaskStatus.IN_PROGRESS,
                    owner=normalized_owner,
                )
                proposal = self._replace_task(current, claimed)
                self._commit(current, proposal)
                return {"changed": True, **self._task_details(claimed, proposal)}
            except TaskStoreError:
                raise
            except OSError as exc:
                raise self._storage_error() from exc

    def complete_task(self, id: object, owner: object) -> dict[str, Any]:
        """Atomically complete an owned in-progress task."""
        with self._lock:
            try:
                task_id = self._validate_input_id(id)
                normalized_owner = self._validate_input_string(
                    owner,
                    field="owner",
                    maximum=MAX_TASK_OWNER_CHARS,
                    code="invalid_task_owner",
                )
                current = self._read_tasks()
                task = self._require_task(current, task_id)

                if task.status is TaskStatus.COMPLETED:
                    if task.owner == normalized_owner:
                        return {
                            "changed": False,
                            **self._task_details(task, current),
                        }
                    raise TaskStoreError(
                        "task_owner_mismatch",
                        "Task owner does not match the completing owner",
                    )
                if task.status is TaskStatus.PENDING:
                    raise TaskStoreError(
                        "invalid_task_transition",
                        "A pending task must be claimed before completion",
                    )
                if task.owner != normalized_owner:
                    raise TaskStoreError(
                        "task_owner_mismatch",
                        "Task owner does not match the completing owner",
                    )

                completed = replace(task, status=TaskStatus.COMPLETED)
                proposal = self._replace_task(current, completed)
                self._commit(current, proposal)
                return {"changed": True, **self._task_details(completed, proposal)}
            except TaskStoreError:
                raise
            except OSError as exc:
                raise self._storage_error() from exc

    def list_tasks(self) -> list[dict[str, Any]]:
        """Return stable, compact summaries of every task."""
        with self._lock:
            try:
                tasks = self._read_tasks()
                return [self._task_summary(task, tasks) for task in tasks]
            except TaskStoreError:
                raise
            except OSError as exc:
                raise self._storage_error() from exc

    def get_task(self, id: object) -> dict[str, Any]:
        """Return all persisted fields and current executability for one task."""
        with self._lock:
            try:
                task_id = self._validate_input_id(id)
                tasks = self._read_tasks()
                task = self._require_task(tasks, task_id)
                return self._task_details(task, tasks)
            except TaskStoreError:
                raise
            except OSError as exc:
                raise self._storage_error() from exc

    def _read_tasks(self) -> tuple[Task, ...]:
        self._validate_read_boundary()
        if not self.path.exists():
            tasks: tuple[Task, ...] = ()
            self._validate_snapshot(tasks)
            return tasks

        with self.path.open("rb") as stream:
            data = stream.read(MAX_TASK_FILE_BYTES + 1)
        if len(data) > MAX_TASK_FILE_BYTES:
            raise self._corrupt_error()
        try:
            text = data.decode("utf-8")
            document = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self._corrupt_error() from exc
        return self._parse_document(document)

    def _parse_document(self, document: object) -> tuple[Task, ...]:
        if not isinstance(document, dict) or set(document) != _DOCUMENT_KEYS:
            raise self._corrupt_error()
        schema_version = document.get("schema_version")
        if schema_version != TASK_SCHEMA_VERSION or isinstance(schema_version, bool):
            raise self._corrupt_error()
        raw_tasks = document.get("tasks")
        if not isinstance(raw_tasks, list) or len(raw_tasks) > MAX_TASKS:
            raise self._corrupt_error()

        tasks: list[Task] = []
        for raw_task in raw_tasks:
            if not isinstance(raw_task, dict) or set(raw_task) != _TASK_KEYS:
                raise self._corrupt_error()
            task_id = raw_task.get("id")
            name = raw_task.get("name")
            summary = raw_task.get("summary")
            topic = raw_task.get("topic")
            owner = raw_task.get("owner")
            dependencies = raw_task.get("dependencies")
            if not isinstance(task_id, str) or not _TASK_ID_PATTERN.fullmatch(task_id):
                raise self._corrupt_error()
            if not self._is_stored_string(name, MAX_TASK_NAME_CHARS):
                raise self._corrupt_error()
            if not self._is_stored_string(summary, MAX_TASK_SUMMARY_CHARS):
                raise self._corrupt_error()
            if not self._is_stored_string(topic, MAX_TASK_TOPIC_CHARS):
                raise self._corrupt_error()
            if owner is not None and not self._is_stored_string(
                owner, MAX_TASK_OWNER_CHARS
            ):
                raise self._corrupt_error()
            if (
                not isinstance(dependencies, list)
                or len(dependencies) > MAX_TASK_DEPENDENCIES
                or any(
                    not isinstance(dependency, str)
                    or not _TASK_ID_PATTERN.fullmatch(dependency)
                    for dependency in dependencies
                )
            ):
                raise self._corrupt_error()
            try:
                status = TaskStatus(raw_task.get("status"))
            except (TypeError, ValueError) as exc:
                raise self._corrupt_error() from exc
            tasks.append(
                Task(
                    id=task_id,
                    name=name,
                    status=status,
                    summary=summary,
                    topic=topic,
                    owner=owner,
                    dependencies=tuple(dependencies),
                )
            )

        result = tuple(sorted(tasks, key=lambda task: task.id))
        self._validate_snapshot(result)
        return result

    def _validate_snapshot(self, tasks: tuple[Task, ...]) -> None:
        if len(tasks) > MAX_TASKS:
            raise self._corrupt_error()
        by_id: dict[str, Task] = {}
        for task in tasks:
            if not isinstance(task, Task):
                raise self._corrupt_error()
            if task.id in by_id or not _TASK_ID_PATTERN.fullmatch(task.id):
                raise self._corrupt_error()
            by_id[task.id] = task
            if not self._is_stored_string(task.name, MAX_TASK_NAME_CHARS):
                raise self._corrupt_error()
            if not self._is_stored_string(task.summary, MAX_TASK_SUMMARY_CHARS):
                raise self._corrupt_error()
            if not self._is_stored_string(task.topic, MAX_TASK_TOPIC_CHARS):
                raise self._corrupt_error()
            if task.owner is not None and not self._is_stored_string(
                task.owner, MAX_TASK_OWNER_CHARS
            ):
                raise self._corrupt_error()
            if not isinstance(task.status, TaskStatus):
                raise self._corrupt_error()
            if len(task.dependencies) > MAX_TASK_DEPENDENCIES:
                raise self._corrupt_error()
            if len(set(task.dependencies)) != len(task.dependencies):
                raise self._corrupt_error()
            if task.id in task.dependencies:
                raise self._corrupt_error()
            if task.status is TaskStatus.PENDING and task.owner is not None:
                raise self._corrupt_error()
            if task.status is not TaskStatus.PENDING and task.owner is None:
                raise self._corrupt_error()

        for task in tasks:
            if any(dependency not in by_id for dependency in task.dependencies):
                raise self._corrupt_error()

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise self._corrupt_error()
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in by_id[task_id].dependencies:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in sorted(by_id):
            visit(task_id)

    def _commit(
        self,
        current: tuple[Task, ...],
        proposal: tuple[Task, ...],
    ) -> None:
        self._validate_snapshot(current)
        proposal = tuple(sorted(proposal, key=lambda task: task.id))
        self._validate_snapshot(proposal)
        self._validate_proposal_transition(current, proposal)
        serialized = self._serialize(proposal)
        if len(serialized.encode("utf-8")) > MAX_TASK_FILE_BYTES:
            raise TaskStoreError(
                "task_limit_reached",
                "Task storage file limit has been reached",
            )

        # Validate the exact bytes that will become authoritative before writing.
        try:
            round_tripped = json.loads(serialized)
        except json.JSONDecodeError as exc:  # pragma: no cover - json.dumps invariant
            raise self._corrupt_error() from exc
        if self._parse_document(round_tripped) != proposal:
            raise self._corrupt_error()

        self._ensure_write_layout()
        self._atomic_write(self.path, serialized)

    def _validate_proposal_transition(
        self,
        current: tuple[Task, ...],
        proposal: tuple[Task, ...],
    ) -> None:
        current_by_id = {task.id: task for task in current}
        proposal_by_id = {task.id: task for task in proposal}
        if not set(current_by_id).issubset(proposal_by_id):
            raise self._corrupt_error()
        if len(proposal) not in {len(current), len(current) + 1}:
            raise self._corrupt_error()

        for task_id, old in current_by_id.items():
            new = proposal_by_id[task_id]
            if old.dependencies != new.dependencies:
                raise self._corrupt_error()
            if old.status is TaskStatus.COMPLETED and new != old:
                raise self._corrupt_error()
            if old == new:
                continue
            if (
                old.status is TaskStatus.PENDING
                and old.owner is None
                and new.status is TaskStatus.IN_PROGRESS
                and new.owner is not None
                and self._same_non_lifecycle_fields(old, new)
            ):
                continue
            if (
                old.status is TaskStatus.IN_PROGRESS
                and new.status is TaskStatus.COMPLETED
                and old.owner == new.owner
                and self._same_non_lifecycle_fields(old, new)
            ):
                continue
            raise self._corrupt_error()

        for task_id in set(proposal_by_id).difference(current_by_id):
            task = proposal_by_id[task_id]
            if task.status is not TaskStatus.PENDING or task.owner is not None:
                raise self._corrupt_error()

    @staticmethod
    def _same_non_lifecycle_fields(old: Task, new: Task) -> bool:
        return (
            old.id == new.id
            and old.name == new.name
            and old.summary == new.summary
            and old.topic == new.topic
            and old.dependencies == new.dependencies
        )

    @staticmethod
    def _serialize(tasks: Iterable[Task]) -> str:
        return json.dumps(
            {
                "schema_version": TASK_SCHEMA_VERSION,
                "tasks": [task.as_dict() for task in tasks],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"

    def _validate_read_boundary(self) -> None:
        metadata_root = self.root.parent
        resolved_metadata = self._resolved_directory(metadata_root)
        if resolved_metadata is None:
            return
        resolved_root = self._resolved_directory(self.root)
        if resolved_root is None:
            return
        if self.path.is_symlink():
            raise self._storage_error()
        if self.path.exists():
            try:
                resolved_path = self.path.resolve(strict=True)
            except OSError as exc:
                raise self._storage_error() from exc
            if not resolved_path.is_file() or not self._is_within(
                resolved_path, resolved_root
            ):
                raise self._storage_error()

    def _ensure_write_layout(self) -> None:
        for directory in (self.root.parent, self.root):
            resolved = self._resolved_directory(directory)
            if resolved is None:
                directory.mkdir()
                resolved = self._resolved_directory(directory)
            if resolved is None:  # pragma: no cover - defensive race guard
                raise self._storage_error()
        self._validate_read_boundary()

    def _resolved_directory(self, directory: Path) -> Path | None:
        if not directory.exists() and not directory.is_symlink():
            return None
        try:
            resolved = directory.resolve(strict=True)
        except OSError as exc:
            raise self._storage_error() from exc
        if not resolved.is_dir() or not self._is_within(
            resolved, self.workspace_root
        ):
            raise self._storage_error()
        return resolved

    @staticmethod
    def _atomic_write(target: Path, content: str) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        descriptor_open = True
        try:
            with os.fdopen(
                descriptor,
                "w",
                encoding="utf-8",
                newline="\n",
            ) as stream:
                descriptor_open = False
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, target)
        except BaseException:
            if descriptor_open:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass
            raise

    @staticmethod
    def _validate_input_string(
        value: object,
        *,
        field: str,
        maximum: int,
        code: str,
    ) -> str:
        if not isinstance(value, str) or not value.strip():
            raise TaskStoreError(code, f"{field} must be a non-empty string")
        normalized = value.strip()
        if len(normalized) > maximum:
            raise TaskStoreError(code, f"{field} exceeds {maximum} characters")
        return normalized

    @staticmethod
    def _validate_input_id(value: object) -> str:
        if not isinstance(value, str) or not _TASK_ID_PATTERN.fullmatch(value):
            raise TaskStoreError(
                "invalid_task_id",
                "id must be a 32-character lowercase hexadecimal string",
            )
        return value

    @staticmethod
    def _validate_input_dependencies(value: object) -> tuple[str, ...]:
        if not isinstance(value, list):
            raise TaskStoreError(
                "invalid_task_dependencies",
                "dependencies must be an array of task IDs",
            )
        if len(value) > MAX_TASK_DEPENDENCIES:
            raise TaskStoreError(
                "invalid_task_dependencies",
                f"dependencies may contain at most {MAX_TASK_DEPENDENCIES} IDs",
            )
        dependencies: list[str] = []
        for dependency in value:
            if not isinstance(dependency, str) or not _TASK_ID_PATTERN.fullmatch(
                dependency
            ):
                raise TaskStoreError(
                    "invalid_task_dependencies",
                    "dependencies must contain valid task IDs",
                )
            dependencies.append(dependency)
        if len(set(dependencies)) != len(dependencies):
            raise TaskStoreError(
                "invalid_task_dependencies",
                "dependencies must not contain duplicate task IDs",
            )
        return tuple(dependencies)

    def _new_task_id(self, current_by_id: Mapping[str, Task]) -> str:
        for _ in range(100):
            candidate = self._id_factory()
            if (
                isinstance(candidate, str)
                and _TASK_ID_PATTERN.fullmatch(candidate)
                and candidate not in current_by_id
            ):
                return candidate
        raise self._storage_error()

    @staticmethod
    def _require_task(tasks: tuple[Task, ...], task_id: str) -> Task:
        for task in tasks:
            if task.id == task_id:
                return task
        raise TaskStoreError("task_not_found", "Task does not exist")

    @staticmethod
    def _replace_task(tasks: tuple[Task, ...], replacement: Task) -> tuple[Task, ...]:
        return tuple(
            replacement if task.id == replacement.id else task for task in tasks
        )

    @staticmethod
    def _executability(task: Task, tasks: tuple[Task, ...]) -> _Executability:
        """The single authoritative ready-to-claim rule used by every API."""
        if task.status is TaskStatus.IN_PROGRESS:
            return _Executability(False, "in_progress")
        if task.status is TaskStatus.COMPLETED:
            return _Executability(False, "completed")
        by_id = {candidate.id: candidate for candidate in tasks}
        blocking = tuple(
            dependency
            for dependency in task.dependencies
            if by_id[dependency].status is not TaskStatus.COMPLETED
        )
        if blocking:
            return _Executability(False, "blocked_by_dependencies", blocking)
        return _Executability(True, "ready")

    def _task_details(
        self,
        task: Task,
        tasks: tuple[Task, ...],
    ) -> dict[str, Any]:
        return {**task.as_dict(), **self._executability(task, tasks).as_dict()}

    def _task_summary(
        self,
        task: Task,
        tasks: tuple[Task, ...],
    ) -> dict[str, Any]:
        executability = self._executability(task, tasks)
        return {
            "id": task.id,
            "name": task.name,
            "status": task.status.value,
            "summary": task.summary,
            "topic": task.topic,
            "owner": task.owner,
            "executable": executability.executable,
            "reason": executability.reason,
            "dependency_count": len(task.dependencies),
            "blocking_dependency_ids": list(
                executability.blocking_dependency_ids
            ),
        }

    @staticmethod
    def _is_stored_string(value: object, maximum: int) -> bool:
        return (
            isinstance(value, str)
            and bool(value)
            and value == value.strip()
            and len(value) <= maximum
        )

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    @staticmethod
    def _corrupt_error() -> TaskStoreError:
        return TaskStoreError(
            "task_store_corrupt",
            "Task storage contains an invalid snapshot",
        )

    @staticmethod
    def _storage_error() -> TaskStoreError:
        return TaskStoreError(
            "task_storage_error",
            "Task storage could not be accessed safely",
        )


def task_tools(store: TaskStore) -> list[FunctionTool]:
    """Adapt one shared TaskStore to six strict FunctionTool APIs."""

    if not isinstance(store, TaskStore):
        raise TypeError("store must be a TaskStore")

    def create_task(
        name: str,
        summary: str,
        topic: str,
        dependencies: list[str],
    ) -> dict[str, Any]:
        try:
            result = store.create_task(name, summary, topic, dependencies)
        except TaskStoreError as exc:
            return _error_result(exc)
        return {"ok": True, "created": True, **result}

    def is_task_executable(id: str) -> dict[str, Any]:
        try:
            result = store.is_task_executable(id)
        except TaskStoreError as exc:
            return _error_result(exc)
        return {"ok": True, **result}

    def claim_task(id: str, owner: str) -> dict[str, Any]:
        try:
            result = store.claim_task(id, owner)
        except TaskStoreError as exc:
            return _error_result(exc)
        return {"ok": True, **result}

    def complete_task(id: str, owner: str) -> dict[str, Any]:
        try:
            result = store.complete_task(id, owner)
        except TaskStoreError as exc:
            return _error_result(exc)
        return {"ok": True, **result}

    def list_tasks() -> dict[str, Any]:
        try:
            tasks = store.list_tasks()
        except TaskStoreError as exc:
            return _error_result(exc)
        return {"ok": True, "tasks": tasks, "count": len(tasks)}

    def get_task(id: str) -> dict[str, Any]:
        try:
            result = store.get_task(id)
        except TaskStoreError as exc:
            return _error_result(exc)
        return {"ok": True, **result}

    id_property = {
        "type": "string",
        "pattern": _TASK_ID_PATTERN.pattern,
        "minLength": 32,
        "maxLength": 32,
    }
    owner_property = {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_TASK_OWNER_CHARS,
    }
    empty_parameters = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    return [
        FunctionTool(
            name=CREATE_TASK_TOOL,
            description=(
                "Create a workspace-persistent pending DAG task. Every dependency "
                "must already exist; the Store generates the task ID."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_TASK_NAME_CHARS,
                    },
                    "summary": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_TASK_SUMMARY_CHARS,
                    },
                    "topic": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_TASK_TOPIC_CHARS,
                    },
                    "dependencies": {
                        "type": "array",
                        "maxItems": MAX_TASK_DEPENDENCIES,
                        "uniqueItems": True,
                        "items": id_property,
                    },
                },
                "required": ["name", "summary", "topic", "dependencies"],
                "additionalProperties": False,
            },
            handler=create_task,
            strict=True,
        ),
        FunctionTool(
            name=IS_TASK_EXECUTABLE_TOOL,
            description=(
                "Check whether a persistent task is pending, unowned, and has only "
                "completed dependencies, so it can be claimed now."
            ),
            parameters={
                "type": "object",
                "properties": {"id": id_property},
                "required": ["id"],
                "additionalProperties": False,
            },
            handler=is_task_executable,
            strict=True,
        ),
        FunctionTool(
            name=CLAIM_TASK_TOOL,
            description=(
                "Claim an executable persistent task for an explicit Agent label. "
                "The owner is collaboration metadata, not an authenticated identity."
            ),
            parameters={
                "type": "object",
                "properties": {"id": id_property, "owner": owner_property},
                "required": ["id", "owner"],
                "additionalProperties": False,
            },
            handler=claim_task,
            strict=True,
        ),
        FunctionTool(
            name=COMPLETE_TASK_TOOL,
            description=(
                "Complete an in-progress persistent task when the supplied Agent "
                "label exactly matches its owner."
            ),
            parameters={
                "type": "object",
                "properties": {"id": id_property, "owner": owner_property},
                "required": ["id", "owner"],
                "additionalProperties": False,
            },
            handler=complete_task,
            strict=True,
        ),
        FunctionTool(
            name=LIST_TASKS_TOOL,
            description=(
                "List stable summaries and current DAG blocking state for all "
                "workspace-persistent tasks."
            ),
            parameters=empty_parameters,
            handler=list_tasks,
            strict=True,
        ),
        FunctionTool(
            name=GET_TASK_TOOL,
            description=(
                "Get every persisted field and current executability for one "
                "workspace task."
            ),
            parameters={
                "type": "object",
                "properties": {"id": id_property},
                "required": ["id"],
                "additionalProperties": False,
            },
            handler=get_task,
            strict=True,
        ),
    ]


def _error_result(error: TaskStoreError) -> dict[str, Any]:
    return {
        "ok": False,
        "code": error.code,
        "error": error.error,
        **error.details,
    }
