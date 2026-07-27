"""Local tools exposed to the agent."""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .filesystem import WorkspaceFiles, filesystem_tools
from .tooling import FunctionTool, ToolRegistry


BASH_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "bash",
    "description": (
        "Run a Bash command in the agent's working directory. Returns JSON with "
        "ok, exit_code, stdout, stderr, and error fields. Commands equivalent to "
        "rm -rf are refused by a temporary hard-coded safety rule."
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

_RM_WRAPPERS = {"command", "sudo"}


def _is_command_separator(token: str) -> bool:
    return bool(token) and all(character in ";&|()" for character in token)


def is_dangerous_command(command: str) -> bool:
    """Return True when a command contains an `rm` with both -r and -f flags."""
    try:
        lexer = shlex.shlex(
            command.replace("\r\n", "\n").replace("\r", "\n").replace("\n", ";"),
            posix=True,
            punctuation_chars=";&|()",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        # Malformed shell input is left for Bash to diagnose.
        return False

    index = 0
    while index < len(tokens):
        while index < len(tokens) and _is_command_separator(tokens[index]):
            index += 1
        if index >= len(tokens):
            break

        token = tokens[index]
        while token in _RM_WRAPPERS and index + 1 < len(tokens):
            index += 1
            token = tokens[index]

        if Path(token).name == "rm":
            recursive = False
            force = False
            index += 1
            while index < len(tokens) and not _is_command_separator(tokens[index]):
                option = tokens[index]
                if option == "--":
                    break
                if option == "--recursive":
                    recursive = True
                elif option == "--force":
                    force = True
                elif option.startswith("-") and not option.startswith("--"):
                    flags = option[1:]
                    recursive = recursive or "r" in flags or "R" in flags
                    force = force or "f" in flags

                if recursive and force:
                    return True
                index += 1

        # Skip the remaining arguments in this command segment. This prevents
        # text such as `echo rm -rf ...` from being treated as a command.
        while index < len(tokens) and not _is_command_separator(tokens[index]):
            index += 1

    return False


class BashTool:
    """Execute Bash commands with a minimal temporary safety check."""

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


def build_default_tool_registry(
    *,
    cwd: str | os.PathLike[str] | None = None,
    bash_tool: Callable[[str], dict[str, Any]] | None = None,
) -> ToolRegistry:
    """Create the standard Bash and workspace-file tool set."""
    workspace = WorkspaceFiles(cwd)
    bash_handler = bash_tool if bash_tool is not None else BashTool(cwd=workspace.root)

    def execute_bash(command: str) -> dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            return {"ok": False, "error": "bash requires a non-empty command"}
        return bash_handler(command)

    bash_function = FunctionTool(
        name="bash",
        description=BASH_TOOL["description"],
        parameters=BASH_TOOL["parameters"],
        handler=execute_bash,
        strict=True,
    )
    return ToolRegistry([bash_function, *filesystem_tools(workspace)])
