"""Permission decisions and user approval for local tool calls."""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from .hooks import PreToolUse
from .long_term_memory import (
    DELETE_MEMORY_TOOL,
    LONG_TERM_MEMORY_TOOL_NAMES,
    ORGANIZE_MEMORY_TOOL,
    STORE_MEMORY_TOOL,
    UPDATE_MEMORY_TOOL,
)
from .memory import LOAD_MEMORY_TOOL
from .skills import (
    ADD_SKILL_TOOL,
    DELETE_SKILL_TOOL,
    SKILL_TOOL_NAMES,
    UPDATE_SKILL_TOOL,
)
from .subagents import SUBAGENT_TOOL_NAMES
from .tasks import (
    CLAIM_TASK_TOOL,
    COMPLETE_TASK_TOOL,
    CREATE_TASK_TOOL,
    TASK_TOOL_NAMES,
)


DEFAULT_TOOL_ALLOWLIST = frozenset(
    {
        "bash",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "update_todo_list",
        "get_todo_list",
        "record_todo_verification",
        LOAD_MEMORY_TOOL,
        *LONG_TERM_MEMORY_TOOL_NAMES,
        *SKILL_TOOL_NAMES,
        *TASK_TOOL_NAMES,
        *SUBAGENT_TOOL_NAMES,
    }
)

_SENSITIVE_FILE_TOOLS = {
    "write_file": "writing a file can create or replace workspace content",
    "edit_file": "editing a file changes workspace content",
}
_SENSITIVE_SKILL_TOOLS = {
    ADD_SKILL_TOOL: "adding a Skill persists instructions in the workspace",
    UPDATE_SKILL_TOOL: "updating a Skill overwrites persistent instructions",
    DELETE_SKILL_TOOL: "deleting a Skill removes persistent instructions",
}
_SENSITIVE_LONG_TERM_MEMORY_TOOLS = {
    STORE_MEMORY_TOOL: "storing a memory persistently changes workspace state",
    UPDATE_MEMORY_TOOL: "updating a memory replaces persistent workspace state",
    DELETE_MEMORY_TOOL: "deleting a memory removes persistent workspace state",
    ORGANIZE_MEMORY_TOOL: (
        "organizing memories can update and delete multiple persistent entries"
    ),
}
_SENSITIVE_TASK_TOOLS = {
    CREATE_TASK_TOOL: "creating a task persists workspace task state",
    CLAIM_TASK_TOOL: "claiming a task changes persistent workspace task state",
    COMPLETE_TASK_TOOL: "completing a task changes persistent workspace task state",
}
_SENSITIVE_COMMANDS = {
    "chmod",
    "chown",
    "dd",
    "fdisk",
    "kill",
    "killall",
    "mkfs",
    "mv",
    "parted",
    "pkill",
    "rmdir",
    "shred",
    "truncate",
    "unlink",
}
_COMMAND_WRAPPERS = {"command", "env", "sudo"}
_WRAPPER_OPTIONS_WITH_VALUE = {
    "-C",
    "--chdir",
    "--close-from",
    "--command-timeout",
    "--group",
    "--host",
    "--prompt",
    "--user",
    "-D",
    "-g",
    "-h",
    "-p",
    "-R",
    "-T",
    "-u",
}
_ENVIRONMENT_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


class PermissionLevel(str, Enum):
    """The three possible outcomes of a permission policy check."""

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionDecision:
    """A policy decision with a user-facing explanation."""

    level: PermissionLevel
    reason: str


@dataclass(frozen=True)
class ApprovalRequest:
    """Information shown to the human before a sensitive tool call."""

    tool_name: str
    arguments: Mapping[str, Any]
    reason: str


ApprovalCallback = Callable[[ApprovalRequest], bool]


class PermissionPolicy(Protocol):
    """Interface used by the permission manager and tool registry."""

    def is_tool_allowed(self, tool_name: str) -> bool: ...

    def evaluate(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> PermissionDecision:
        ...


def _tokenize_command(command: str) -> list[list[str]]:
    """Split a shell command into simple command segments for policy checks."""
    try:
        lexer = shlex.shlex(
            command.replace("\r\n", "\n").replace("\r", "\n").replace("\n", ";"),
            posix=True,
            punctuation_chars=";&|()<>",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        # Bash will report malformed quoting. It is not itself evidence that the
        # command belongs to a destructive category.
        return []

    segments: list[list[str]] = []
    segment: list[str] = []
    for token in tokens:
        if token and all(character in ";&|()" for character in token):
            if segment:
                segments.append(segment)
                segment = []
            continue
        segment.append(token)
    if segment:
        segments.append(segment)
    return segments


def _unwrap_command(segment: list[str]) -> tuple[str, list[str]] | None:
    """Return the executable and its arguments after common shell wrappers."""
    index = 0
    while index < len(segment) and _ENVIRONMENT_ASSIGNMENT.match(segment[index]):
        index += 1

    while index < len(segment):
        executable = Path(segment[index]).name
        if executable not in _COMMAND_WRAPPERS:
            return executable, segment[index + 1 :]
        index += 1
        while index < len(segment):
            token = segment[index]
            if token == "--":
                index += 1
                break
            if executable == "env" and _ENVIRONMENT_ASSIGNMENT.match(token):
                index += 1
                continue
            if token in _WRAPPER_OPTIONS_WITH_VALUE:
                index += 2
                continue
            if token.startswith("-"):
                index += 1
                continue
            break
    return None


def _rm_is_recursive_and_forced(arguments: Iterable[str]) -> bool:
    recursive = False
    force = False
    for option in arguments:
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
    return False


def _git_requires_approval(arguments: list[str]) -> bool:
    if not arguments:
        return False
    subcommand = arguments[0]
    options = set(arguments[1:])
    if subcommand in {"clean", "restore"}:
        return True
    if subcommand == "reset" and "--hard" in options:
        return True
    if subcommand == "checkout" and ({"--", "-f", "--force"} & options):
        return True
    if subcommand == "branch" and "-D" in options:
        return True
    return subcommand == "push" and bool(
        {"--delete", "--force", "-f", "--force-with-lease"} & options
    )


def classify_bash_command(command: str) -> PermissionDecision:
    """Classify common irreversible Bash operations.

    This is a deliberately bounded command guard, not a complete shell sandbox.
    """
    approval_reason: str | None = None
    for segment in _tokenize_command(command):
        if any(">" in token for token in segment):
            approval_reason = (
                approval_reason
                or "shell output redirection can create or overwrite a file"
            )

        unwrapped = _unwrap_command(segment)
        if unwrapped is None:
            continue
        executable, arguments = unwrapped

        if executable == "rm":
            if _rm_is_recursive_and_forced(arguments):
                return PermissionDecision(
                    PermissionLevel.DENY,
                    "recursive forced deletion via rm is permanently forbidden",
                )
            approval_reason = approval_reason or "rm deletes files or directories"
            continue

        if executable in _SENSITIVE_COMMANDS:
            approval_reason = approval_reason or (
                f"{executable} can make a destructive or irreversible change"
            )
            continue

        if executable == "git" and _git_requires_approval(arguments):
            approval_reason = approval_reason or (
                "the requested git operation can discard or overwrite data"
            )
            continue

        if executable == "tee" or (
            executable in {"sed", "perl"}
            and any(option == "-i" or option.startswith("-i") for option in arguments)
        ):
            approval_reason = (
                approval_reason or f"{executable} can overwrite file content"
            )

    if approval_reason is not None:
        return PermissionDecision(PermissionLevel.REQUIRE_APPROVAL, approval_reason)
    return PermissionDecision(PermissionLevel.ALLOW, "operation is allowed by policy")


class DefaultPermissionPolicy:
    """Apply an explicit tool allowlist and classify sensitive built-in calls."""

    def __init__(self, allowed_tools: Iterable[str] | None = None) -> None:
        if isinstance(allowed_tools, str):
            raise TypeError(
                "allowed_tools must be an iterable of tool names, not a string"
            )
        selected = DEFAULT_TOOL_ALLOWLIST if allowed_tools is None else allowed_tools
        self.allowed_tools = frozenset(selected)
        if any(not isinstance(name, str) or not name for name in self.allowed_tools):
            raise ValueError("allowed_tools must contain non-empty strings")

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Return whether a tool may be exposed and considered for execution."""
        return tool_name in self.allowed_tools

    def evaluate(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> PermissionDecision:
        """Return the policy decision for one parsed tool call."""
        if not self.is_tool_allowed(tool_name):
            return PermissionDecision(
                PermissionLevel.DENY,
                f"tool {tool_name!r} is not in the allowlist",
            )

        if tool_name in _SENSITIVE_FILE_TOOLS:
            return PermissionDecision(
                PermissionLevel.REQUIRE_APPROVAL,
                _SENSITIVE_FILE_TOOLS[tool_name],
            )

        if tool_name in _SENSITIVE_SKILL_TOOLS:
            return PermissionDecision(
                PermissionLevel.REQUIRE_APPROVAL,
                _SENSITIVE_SKILL_TOOLS[tool_name],
            )

        if tool_name in _SENSITIVE_LONG_TERM_MEMORY_TOOLS:
            return PermissionDecision(
                PermissionLevel.REQUIRE_APPROVAL,
                _SENSITIVE_LONG_TERM_MEMORY_TOOLS[tool_name],
            )

        if tool_name in _SENSITIVE_TASK_TOOLS:
            return PermissionDecision(
                PermissionLevel.REQUIRE_APPROVAL,
                _SENSITIVE_TASK_TOOLS[tool_name],
            )

        if tool_name == "bash":
            command = arguments.get("command")
            if isinstance(command, str):
                return classify_bash_command(command)

        return PermissionDecision(PermissionLevel.ALLOW, "operation is allowed by policy")


class PermissionManager:
    """Combine deterministic policy checks with a human approval callback."""

    def __init__(
        self,
        policy: PermissionPolicy,
        approval_callback: ApprovalCallback | None = None,
    ) -> None:
        self.policy = policy
        self.approval_callback = approval_callback

    def is_tool_allowed(self, tool_name: str) -> bool:
        return self.policy.is_tool_allowed(tool_name)

    def __call__(self, event: PreToolUse) -> None:
        """Keep legacy registry injection working through the Hook interface."""
        _apply_permission_decision(self, event)

    def authorize(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> PermissionDecision:
        """Resolve a policy decision, asking the human only when required."""
        decision = self.policy.evaluate(tool_name, arguments)
        if decision.level is not PermissionLevel.REQUIRE_APPROVAL:
            return decision

        if self.approval_callback is None:
            return PermissionDecision(
                PermissionLevel.DENY,
                f"{decision.reason}; no user approval handler is configured",
            )

        request = ApprovalRequest(tool_name, dict(arguments), decision.reason)
        try:
            approved = self.approval_callback(request)
        except Exception as exc:
            return PermissionDecision(
                PermissionLevel.DENY,
                f"user approval failed: {exc}",
            )

        if approved:
            return PermissionDecision(PermissionLevel.ALLOW, "approved by the user")
        return PermissionDecision(
            PermissionLevel.DENY,
            f"{decision.reason}; the user did not approve this operation",
        )


class PermissionHook:
    """Apply a ``PermissionManager`` as the built-in ``PreToolUse`` hook."""

    def __init__(self, manager: PermissionManager) -> None:
        self.manager = manager

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Expose the same visibility rule used by execution authorization."""
        return self.manager.is_tool_allowed(tool_name)

    def __call__(self, event: PreToolUse) -> None:
        _apply_permission_decision(self.manager, event)


def _apply_permission_decision(
    manager: PermissionManager,
    event: PreToolUse,
) -> None:
    decision = manager.authorize(event.tool_name, event.arguments)
    event.permission_level = decision.level.value
    event.permission_reason = decision.reason
    if decision.level is PermissionLevel.DENY:
        event.deny(
            decision.reason,
            result={
                "ok": False,
                "code": "permission_denied",
                "error": f"Permission denied: {decision.reason}",
                "permission": "denied",
            },
        )
