"""Local tools exposed to the agent."""

from __future__ import annotations

import os
import secrets
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any

from .filesystem import WorkspaceRoot
from .permissions import (
    BACKGROUND_BASH_TOOL,
    PermissionLevel,
    classify_bash_command,
)
from .tooling import FunctionTool

if TYPE_CHECKING:
    from .hooks import HookRegistry
    from .permissions import ApprovalCallback
    from .todo import TodoList
    from .tooling import ToolRegistry


BASH_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "bash",
    "description": (
        "Run a Bash command in the agent's working directory. Returns JSON with "
        "ok, exit_code, stdout, stderr, and error fields. Permanently forbidden "
        "operations are refused, and sensitive operations require explicit user "
        "approval."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The complete Bash command to execute.",
            }
        },
        "required": ["command"],
        "additionalProperties": False,
    },
    "strict": True,
}

_BACKGROUND_ERROR_CHARS = 1_000
_BASH_OVERRIDE_ENV = "MYAGENT_BASH"
_PROCESS_TREE_CLEANUP_SECONDS = 5


class BackgroundBashRunner:
    """Own a bounded set of Bash calls whose results are harvested by AgentLoop."""

    def __init__(
        self,
        handler: Callable[[str], dict[str, Any]],
        *,
        max_workers: int = 2,
        max_tasks: int = 8,
    ) -> None:
        if not callable(handler):
            raise TypeError("handler must be callable")
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if max_tasks <= 0:
            raise ValueError("max_tasks must be positive")
        self._handler = handler
        self._max_tasks = max_tasks
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="myagent-background-bash",
        )
        self._lock = Lock()
        self._tasks: list[tuple[str, Future[dict[str, Any]]]] = []
        self._closed = False

    def submit(self, command: str) -> dict[str, Any]:
        """Submit one already-authorized command without waiting for its result."""
        if not isinstance(command, str) or not command.strip():
            return _background_error("invalid_command", "command must be non-empty")
        with self._lock:
            if self._closed:
                return _background_error("runner_closed", "background Bash is closed")
            if len(self._tasks) >= self._max_tasks:
                return _background_error(
                    "task_limit_reached",
                    f"at most {self._max_tasks} unharvested background tasks are allowed",
                )
            task_id = secrets.token_urlsafe(18)
            try:
                future = self._executor.submit(self._handler, command)
            except RuntimeError:
                return _background_error(
                    "runner_closed",
                    "background Bash is closed",
                )
            self._tasks.append((task_id, future))
        return {
            "ok": True,
            "status": "submitted",
            "background_task_id": task_id,
        }

    def drain_completed(self) -> list[dict[str, Any]]:
        """Remove and return every currently completed task in submission order."""
        with self._lock:
            completed = [task for task in self._tasks if task[1].done()]
            if completed:
                completed_ids = {id(future) for _, future in completed}
                self._tasks = [
                    task for task in self._tasks if id(task[1]) not in completed_ids
                ]
        return [self._completed_result(task_id, future) for task_id, future in completed]

    def wait_for_all(self) -> None:
        """Wait locally until every currently tracked task reaches a terminal state."""
        with self._lock:
            futures = [future for _, future in self._tasks]
        if futures:
            wait(futures)

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._tasks)

    def reset_and_discard(self) -> None:
        """Wait for started work and discard every result from the old history."""
        with self._lock:
            futures = [future for _, future in self._tasks]
            if futures:
                wait(futures)
            self._tasks.clear()

    def close(self) -> None:
        """Idempotently reject submissions and shut down all worker threads."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            futures = [future for _, future in self._tasks]
            for future in futures:
                future.cancel()
        self._executor.shutdown(wait=True, cancel_futures=True)

    @staticmethod
    def _completed_result(
        task_id: str,
        future: Future[dict[str, Any]],
    ) -> dict[str, Any]:
        if future.cancelled():
            return {
                "background_task_id": task_id,
                "status": "failed",
                "error": "background Bash task was cancelled before it started",
            }
        try:
            result = future.result()
        except Exception as exc:
            error = str(exc).strip() or type(exc).__name__
            return {
                "background_task_id": task_id,
                "status": "failed",
                "error": _clip_background_error(error),
            }
        if not isinstance(result, dict):
            return {
                "background_task_id": task_id,
                "status": "failed",
                "error": "background Bash handler returned a non-object result",
            }
        return {
            "background_task_id": task_id,
            "status": "completed",
            "result": result,
        }


def is_dangerous_command(command: str) -> bool:
    """Return True only for commands that the policy permanently forbids."""
    return classify_bash_command(command).level is PermissionLevel.DENY


class BashTool:
    """Execute Bash commands with defense-in-depth for permanent denials."""

    def __init__(
        self,
        *,
        cwd: str | os.PathLike[str] | WorkspaceRoot | None = None,
        timeout_seconds: int = 30,
        max_output_chars: int = 50_000,
        bash_executable: str | os.PathLike[str] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_output_chars <= 0:
            raise ValueError("max_output_chars must be positive")

        self._cwd = (
            cwd if isinstance(cwd, WorkspaceRoot) else WorkspaceRoot(cwd or Path.cwd())
        )
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars
        if bash_executable is None:
            self.executable = _resolve_bash_executable()
        else:
            selected = os.fspath(bash_executable)
            if not isinstance(selected, str) or not selected.strip():
                raise ValueError("bash_executable must be a non-empty path")
            self.executable = selected

    @property
    def cwd(self) -> Path:
        return self._cwd.current

    def __call__(self, command: str) -> dict[str, Any]:
        if is_dangerous_command(command):
            return {
                "ok": False,
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "error": "Command refused: recursive forced deletion via rm is blocked.",
            }

        process: subprocess.Popen[str] | None = None
        windows_job: _WindowsProcessJob | None = None
        try:
            popen_options: dict[str, Any] = {}
            if sys.platform == "win32":
                popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_options["start_new_session"] = True

            arguments = [self.executable, "-lc", command]
            if sys.platform == "win32":
                arguments = [
                    self.executable,
                    "--noprofile",
                    "--norc",
                    "-c",
                    command,
                ]

            process = subprocess.Popen(
                arguments,
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                **popen_options,
            )
            if sys.platform == "win32":
                windows_job = _WindowsProcessJob.try_create(process)
            try:
                stdout, stderr = process.communicate(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                cleanup_error = _terminate_process_tree(process, windows_job)
                try:
                    stdout, stderr = process.communicate(
                        timeout=_PROCESS_TREE_CLEANUP_SECONDS
                    )
                except subprocess.TimeoutExpired:
                    stdout = exc.stdout or ""
                    stderr = exc.stderr or ""
                error = f"Command timed out after {self.timeout_seconds} seconds."
                if cleanup_error:
                    error = f"{error} Process cleanup failed: {cleanup_error}"
                return {
                    "ok": False,
                    "exit_code": None,
                    "stdout": self._truncate(stdout),
                    "stderr": self._truncate(stderr),
                    "error": error,
                }
        except FileNotFoundError:
            return {
                "ok": False,
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "error": f"Bash executable was not found: {self.executable}",
            }
        finally:
            if windows_job is not None:
                windows_job.close()

        assert process is not None
        ok = process.returncode == 0
        return {
            "ok": ok,
            "exit_code": process.returncode,
            "stdout": self._truncate(stdout or ""),
            "stderr": self._truncate(stderr or ""),
            "error": (
                None if ok else f"Command exited with code {process.returncode}."
            ),
        }

    def _truncate(self, value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            value = value.decode(errors="replace")
        if len(value) <= self.max_output_chars:
            return value
        omitted = len(value) - self.max_output_chars
        return f"{value[:self.max_output_chars]}\n...[truncated {omitted} characters]"


class _WindowsProcessJob:
    """Own a Windows process tree so timeout cleanup includes descendants."""

    def __init__(self, handle: int, kernel32: Any) -> None:
        self._handle = handle
        self._kernel32 = kernel32

    @classmethod
    def try_create(
        cls,
        process: subprocess.Popen[str],
    ) -> _WindowsProcessJob | None:
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.AssignProcessToJobObject.argtypes = [
                wintypes.HANDLE,
                wintypes.HANDLE,
            ]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel32.TerminateJobObject.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel32.AssignProcessToJobObject(handle, int(process._handle)):
                error = ctypes.get_last_error()
                kernel32.CloseHandle(handle)
                raise ctypes.WinError(error)
            return cls(handle, kernel32)
        except (AttributeError, OSError):
            return None

    def terminate(self) -> str | None:
        import ctypes

        if self._kernel32.TerminateJobObject(self._handle, 1):
            return None
        return str(ctypes.WinError(ctypes.get_last_error()))

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = 0


def _terminate_process_tree(
    process: subprocess.Popen[str],
    windows_job: _WindowsProcessJob | None,
) -> str | None:
    if sys.platform == "win32":
        if windows_job is not None:
            return windows_job.terminate()
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_PROCESS_TREE_CLEANUP_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            if process.poll() is None:
                process.kill()
            return str(exc)
        if completed.returncode != 0 and process.poll() is None:
            process.kill()
        return None

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return None
    except OSError as exc:
        if process.poll() is None:
            process.kill()
        return str(exc)
    return None


def _resolve_bash_executable() -> str:
    override = os.getenv(_BASH_OVERRIDE_ENV, "").strip()
    if override:
        return override
    if sys.platform == "win32":
        for candidate in _windows_git_bash_candidates():
            if candidate.is_file():
                return str(candidate)
    return shutil.which("bash") or "bash"


def _windows_git_bash_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []
    git_executable = shutil.which("git")
    if git_executable:
        for root in tuple(Path(git_executable).parents)[:3]:
            candidates.extend(
                (
                    root / "usr" / "bin" / "bash.exe",
                    root / "bin" / "bash.exe",
                )
            )

    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.getenv(variable)
        if base:
            candidates.extend(
                (
                    Path(base) / "Git" / "usr" / "bin" / "bash.exe",
                    Path(base) / "Git" / "bin" / "bash.exe",
                )
            )
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        root = Path(local_app_data) / "Programs" / "Git"
        candidates.extend(
            (root / "usr" / "bin" / "bash.exe", root / "bin" / "bash.exe")
        )

    return tuple(dict.fromkeys(candidates))


def build_bash_function_tool(
    handler: Callable[[str], dict[str, Any]],
) -> FunctionTool:
    """Bind one Bash handler to the stable public function-tool schema."""

    def execute_bash(command: str) -> dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            return {"ok": False, "error": "bash requires a non-empty command"}
        return handler(command)

    return FunctionTool(
        name="bash",
        description=BASH_TOOL["description"],
        parameters=BASH_TOOL["parameters"],
        handler=execute_bash,
        strict=True,
    )


def build_background_bash_function_tool(
    runner: BackgroundBashRunner,
) -> FunctionTool:
    """Bind a background runner to the default main-agent-only schema."""

    def run_bash_in_background(
        command: str,
        independent_work: str,
    ) -> dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            return _background_error("invalid_command", "command must be non-empty")
        if not isinstance(independent_work, str) or not independent_work.strip():
            return _background_error(
                "invalid_independent_work",
                "independent_work must describe concrete non-empty work",
            )
        return runner.submit(command)

    return FunctionTool(
        name=BACKGROUND_BASH_TOOL,
        description=(
            "Submit a Bash command only when it is expected to block noticeably and "
            "there is concrete valuable independent work to do immediately. The "
            "independent work must not depend on stdout, exit status, side effects, "
            "or share files, processes, or state with the command; the result must "
            "not determine later tool parameters, safety, approval, or user choices. "
            "Use synchronous bash if any condition is false or the command is quick. "
            "After submission, immediately perform independent_work. Do not poll; "
            "the runtime automatically injects untrusted BACKGROUND_TOOL_RESULTS."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The non-empty Bash command expected to block.",
                },
                "independent_work": {
                    "type": "string",
                    "description": (
                        "Concrete valuable work to perform immediately that is fully "
                        "independent of the command result and side effects."
                    ),
                },
            },
            "required": ["command", "independent_work"],
            "additionalProperties": False,
        },
        handler=run_bash_in_background,
        strict=True,
    )


def _background_error(code: str, error: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": error}


def _clip_background_error(error: str) -> str:
    if len(error) <= _BACKGROUND_ERROR_CHARS:
        return error
    return error[: _BACKGROUND_ERROR_CHARS - 1] + "…"


def build_default_tool_registry(
    *,
    cwd: str | os.PathLike[str] | None = None,
    bash_tool: Callable[[str], dict[str, Any]] | None = None,
    allowed_tools: Iterable[str] | None = None,
    approval_callback: ApprovalCallback | None = None,
    hooks: HookRegistry | None = None,
    todo_list: TodoList | None = None,
    todo_reminder_tool_calls: int | None = None,
) -> ToolRegistry:
    """Preserve the original import path for the default composition helper."""
    from .composition import build_default_tool_registry as build_registry

    return build_registry(
        cwd=cwd,
        bash_tool=bash_tool,
        allowed_tools=allowed_tools,
        approval_callback=approval_callback,
        hooks=hooks,
        todo_list=todo_list,
        todo_reminder_tool_calls=todo_reminder_tool_calls,
    )
