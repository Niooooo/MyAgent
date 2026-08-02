"""Reusable primitives for registering and invoking function tools."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from .hooks import HookExecutionError, HookRegistry, PostToolUse, PreToolUse


ToolResult = dict[str, Any]
ToolHandler = Callable[..., ToolResult]


class ToolAccessControl(Protocol):
    """Compatibility interface for access controls attached to a registry."""

    def is_tool_allowed(self, tool_name: str) -> bool: ...

    def __call__(self, event: PreToolUse) -> None: ...


class ToolExecutionError(RuntimeError):
    """An expected error that can be returned to the model safely."""


@dataclass(frozen=True)
class FunctionTool:
    """Pair an OpenAI function-tool definition with its local handler."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    strict: bool = False

    @property
    def definition(self) -> dict[str, Any]:
        """Return the tool schema sent to the Responses API."""
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "strict": self.strict,
        }


class ToolRegistry:
    """Store function tools and provide one guarded execution path."""

    def __init__(
        self,
        tools: Iterable[FunctionTool] = (),
        *,
        tool_visibility: Callable[[str], bool] | None = None,
        hooks: HookRegistry | None = None,
        permission_manager: ToolAccessControl | None = None,
    ) -> None:
        self._tools: dict[str, FunctionTool] = {}
        if tool_visibility is not None and permission_manager is not None:
            raise ValueError(
                "Pass either tool_visibility or legacy permission_manager, not both"
            )
        self._tool_visibility = tool_visibility
        # Keep the attribute and parameter as a migration layer for existing callers.
        self.permission_manager = permission_manager
        self.hooks = hooks or HookRegistry()
        if permission_manager is not None:
            self._tool_visibility = permission_manager.is_tool_allowed
            self.hooks.register(
                PreToolUse,
                permission_manager,
                prepend=True,
            )
        for tool in tools:
            self.register(tool)

    @property
    def definitions(self) -> list[dict[str, Any]]:
        """Return all API definitions in stable registration order."""
        return [
            tool.definition
            for tool in self._tools.values()
            if self._tool_visibility is None or self._tool_visibility(tool.name)
        ]

    def register(self, tool: FunctionTool) -> None:
        """Register a uniquely named tool."""
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def execute(
        self,
        name: str,
        raw_arguments: str,
        *,
        call_id: str | None = None,
    ) -> ToolResult:
        """Decode arguments, call a tool, and keep failures in-band."""
        tool = self._tools.get(name)
        if tool is None:
            return {
                "ok": False,
                "code": "unknown_tool",
                "error": f"Unknown tool: {name}",
            }

        try:
            arguments = json.loads(raw_arguments)
        except (TypeError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "code": "invalid_tool_arguments",
                "error": f"Invalid tool arguments: {exc}",
            }

        if not isinstance(arguments, dict):
            return {
                "ok": False,
                "code": "invalid_tool_arguments",
                "error": "Invalid tool arguments: expected a JSON object",
            }

        try:
            inspect.signature(tool.handler).bind(**arguments)
        except TypeError as exc:
            return {
                "ok": False,
                "code": "invalid_tool_arguments",
                "error": f"Invalid arguments for {name}: {exc}",
            }

        readonly_arguments = MappingProxyType(arguments)
        before = PreToolUse(name, readonly_arguments, call_id)
        pre_tool_result = self._emit_pre_tool_use(before)
        if pre_tool_result is not None:
            return pre_tool_result

        result = self._invoke_handler(tool, arguments)
        after = PostToolUse(name, readonly_arguments, result, call_id)
        return self._emit_post_tool_use(after)

    def _emit_pre_tool_use(self, event: PreToolUse) -> ToolResult | None:
        try:
            self.hooks.emit(event)
        except HookExecutionError as exc:
            return {
                "ok": False,
                "code": "pre_tool_hook_failed",
                "error": str(exc),
                "hook": "PreToolUse",
            }
        if event.denial_reason is None:
            return None
        if event.denial_result is not None:
            return event.denial_result
        return {
            "ok": False,
            "code": "pre_tool_denied",
            "error": f"PreToolUse denied: {event.denial_reason}",
            "hook": "PreToolUse",
        }

    @staticmethod
    def _invoke_handler(
        tool: FunctionTool,
        arguments: dict[str, Any],
    ) -> ToolResult:
        try:
            result = tool.handler(**arguments)
        except ToolExecutionError as exc:
            return {
                "ok": False,
                "code": "tool_execution_error",
                "error": str(exc),
            }
        except Exception as exc:  # Keep unexpected failures inside the loop.
            return {
                "ok": False,
                "code": "tool_execution_failed",
                "error": f"{tool.name} tool failed: {exc}",
            }
        if isinstance(result, dict):
            return result
        return {
            "ok": False,
            "code": "invalid_tool_result",
            "error": f"{tool.name} tool returned a non-object result",
        }

    def _emit_post_tool_use(self, event: PostToolUse) -> ToolResult:
        try:
            self.hooks.emit(event)
        except HookExecutionError as exc:
            return {
                "ok": False,
                "code": "post_tool_hook_failed",
                "error": str(exc),
                "hook": "PostToolUse",
            }
        if isinstance(event.result, dict):
            return event.result
        return {
            "ok": False,
            "code": "invalid_post_tool_result",
            "error": "PostToolUse hook returned a non-object result",
            "hook": "PostToolUse",
        }
