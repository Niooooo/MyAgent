"""Local tools exposed to the agent."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .permissions import PermissionLevel, classify_bash_command
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


def is_dangerous_command(command: str) -> bool:
    """Return True only for commands that the policy permanently forbids."""
    return classify_bash_command(command).level is PermissionLevel.DENY


class BashTool:
    """Execute Bash commands with defense-in-depth for permanent denials."""

    def __init__(
        self,
        *,
        cwd: str | os.PathLike[str] | None = None,
        timeout_seconds: int = 30,
        max_output_chars: int = 50_000,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_output_chars <= 0:
            raise ValueError("max_output_chars must be positive")

        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars

    def __call__(self, command: str) -> dict[str, Any]:
        if is_dangerous_command(command):
            return {
                "ok": False,
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "error": "Command refused: recursive forced deletion via rm is blocked.",
            }

        try:
            completed = subprocess.run(
                ["bash", "-lc", command],
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            return {
                "ok": False,
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "error": "bash executable was not found on PATH.",
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "exit_code": None,
                "stdout": self._truncate(exc.stdout or ""),
                "stderr": self._truncate(exc.stderr or ""),
                "error": f"Command timed out after {self.timeout_seconds} seconds.",
            }

        return {
            "ok": completed.returncode == 0,
            "exit_code": completed.returncode,
            "stdout": self._truncate(completed.stdout),
            "stderr": self._truncate(completed.stderr),
            "error": None,
        }

    def _truncate(self, value: str | bytes) -> str:
        if isinstance(value, bytes):
            value = value.decode(errors="replace")
        if len(value) <= self.max_output_chars:
            return value
        omitted = len(value) - self.max_output_chars
        return f"{value[:self.max_output_chars]}\n...[truncated {omitted} characters]"


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
