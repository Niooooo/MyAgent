"""Task-bound Git worktree lifecycle and FunctionTool adapters."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .tasks import TaskStatus, TaskStore, TaskStoreError
from .tooling import FunctionTool


CREATE_WORKTREE_TOOL = "create_worktree"
DELETE_WORKTREE_TOOL = "delete_worktree"
WORKTREE_TOOL_NAMES = frozenset({CREATE_WORKTREE_TOOL, DELETE_WORKTREE_TOOL})

GitRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


class WorktreeManager:
    """Own Git side effects while TaskStore remains metadata authority."""

    def __init__(
        self,
        repository_root: str | os.PathLike[str],
        task_store: TaskStore,
        *,
        _runner: GitRunner | None = None,
    ) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.managed_root = (
            self.repository_root.parent / f".{self.repository_root.name}-worktrees"
        ).resolve()
        self.task_store = task_store
        self._runner = _runner or self._run_git

    def create(self, id: object) -> dict[str, Any]:
        inspected = self._task(id)
        if not inspected.get("ok"):
            return inspected
        task_id = inspected["id"]
        if inspected.get("status") == TaskStatus.COMPLETED.value:
            return _failure("task_completed", "A completed task cannot gain a worktree")
        if inspected.get("worktree") is not None:
            return _failure("worktree_already_bound", "Task already has a worktree")
        repository = self._verify_repository()
        if repository is not None:
            return repository
        target = self._task_path(task_id)
        if target.exists() or target.is_symlink():
            return _failure("worktree_path_conflict", "Managed worktree path is occupied")
        try:
            self.managed_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return _failure("worktree_storage_error", "Managed worktree root is unavailable")
        added = self._git(["worktree", "add", "--detach", str(target), "HEAD"])
        if added is not None:
            return added
        try:
            bound = self.task_store.bind_worktree(task_id, str(target))
        except TaskStoreError as exc:
            self._git(["worktree", "remove", str(target)])
            return _failure(exc.code, exc.error)
        return {"ok": True, **bound}

    def delete(self, id: object) -> dict[str, Any]:
        inspected = self._task(id)
        if not inspected.get("ok"):
            return inspected
        if inspected.get("status") == TaskStatus.IN_PROGRESS.value:
            return _failure("task_in_progress", "An in-progress task worktree cannot be deleted")
        registered = inspected.get("worktree")
        if registered is None:
            return _failure("worktree_not_bound", "Task does not have a worktree")
        target = Path(registered).resolve()
        if target != self._task_path(inspected["id"]) or not self._within_managed(target):
            return _failure("invalid_worktree_binding", "Task worktree is outside the managed root")
        if not target.is_dir():
            return _failure("worktree_unavailable", "Task worktree directory is unavailable")
        repository = self._verify_repository()
        if repository is not None:
            return repository
        head = self._git_output(["-C", str(target), "rev-parse", "HEAD"])
        if isinstance(head, dict):
            return head
        removed = self._git(["worktree", "remove", str(target)])
        if removed is not None:
            return removed
        try:
            cleared = self.task_store.clear_worktree(inspected["id"], registered)
        except TaskStoreError as exc:
            self._git(["worktree", "add", "--detach", str(target), head])
            return _failure(exc.code, exc.error)
        return {"ok": True, **cleared}

    def _task(self, id: object) -> dict[str, Any]:
        try:
            return {"ok": True, **self.task_store.get_task(id)}
        except TaskStoreError as exc:
            return _failure(exc.code, exc.error)

    def _task_path(self, task_id: str) -> Path:
        target = (self.managed_root / task_id).resolve()
        if not self._within_managed(target):  # validated IDs make this defensive
            raise RuntimeError("invalid managed worktree path")
        return target

    def _within_managed(self, path: Path) -> bool:
        try:
            path.relative_to(self.managed_root)
        except ValueError:
            return False
        return path != self.managed_root

    def _verify_repository(self) -> dict[str, Any] | None:
        try:
            completed = self._runner(
                ["rev-parse", "--show-toplevel"], self.repository_root
            )
        except FileNotFoundError:
            return _failure("git_unavailable", "Git executable is unavailable")
        except (OSError, subprocess.SubprocessError):
            return _failure("git_execution_failed", "Git command could not be executed")
        if completed.returncode != 0:
            return _failure(
                "not_git_repository", "Configured workspace is not a Git repository"
            )
        result = completed.stdout.strip()
        try:
            actual = Path(result).resolve()
        except OSError:
            return _failure("not_git_repository", "Configured workspace is not a Git repository")
        if actual != self.repository_root:
            return _failure("not_git_repository", "Configured workspace is not a Git repository")
        return None

    def _git_output(self, arguments: list[str]) -> str | dict[str, Any]:
        failure = self._invoke(arguments)
        if isinstance(failure, dict):
            return failure
        return failure.stdout.strip()

    def _git(self, arguments: list[str]) -> dict[str, Any] | None:
        result = self._invoke(arguments)
        return result if isinstance(result, dict) else None

    def _invoke(
        self, arguments: list[str]
    ) -> subprocess.CompletedProcess[str] | dict[str, Any]:
        try:
            completed = self._runner(arguments, self.repository_root)
        except FileNotFoundError:
            return _failure("git_unavailable", "Git executable is unavailable")
        except (OSError, subprocess.SubprocessError):
            return _failure("git_execution_failed", "Git command could not be executed")
        if completed.returncode != 0:
            return _failure("git_command_failed", "Git worktree command failed")
        return completed

    @staticmethod
    def _run_git(
        arguments: Sequence[str], cwd: Path
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )


def worktree_tools(manager: WorktreeManager) -> list[FunctionTool]:
    schema = {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "pattern": "^[0-9a-f]{32}$",
                "minLength": 32,
                "maxLength": 32,
            }
        },
        "required": ["id"],
        "additionalProperties": False,
    }
    return [
        FunctionTool(
            name=CREATE_WORKTREE_TOOL,
            description="Create a detached managed Git worktree for a persistent task.",
            parameters=schema,
            handler=manager.create,
            strict=True,
        ),
        FunctionTool(
            name=DELETE_WORKTREE_TOOL,
            description="Remove a clean managed Git worktree from a persistent task.",
            parameters=schema,
            handler=manager.delete,
            strict=True,
        ),
    ]


def _failure(code: str, error: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": error}
