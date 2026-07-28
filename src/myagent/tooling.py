"""Reusable primitives for registering and invoking function tools."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .permissions import PermissionLevel, PermissionManager


ToolResult = dict[str, Any]
ToolHandler = Callable[..., ToolResult]


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
        permission_manager: PermissionManager | None = None,
    ) -> None:
        self._tools: dict[str, FunctionTool] = {}
        self.permission_manager = permission_manager
        for tool in tools:
            self.register(tool)

    @property
    def definitions(self) -> list[dict[str, Any]]:
        """Return all API definitions in stable registration order."""
        return [
            tool.definition
            for tool in self._tools.values()
            if self.permission_manager is None
            or self.permission_manager.is_tool_allowed(tool.name)
        ]

    def register(self, tool: FunctionTool) -> None:
        """Register a uniquely named tool."""
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def execute(self, name: str, raw_arguments: str) -> ToolResult:
        """Decode arguments, call a tool, and keep failures in-band."""
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"Unknown tool: {name}"}

        try:
            arguments = json.loads(raw_arguments)
        except (TypeError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": f"Invalid tool arguments: {exc}"}

        if not isinstance(arguments, dict):
            return {
                "ok": False,
                "error": "Invalid tool arguments: expected a JSON object",
            }

        try:
            inspect.signature(tool.handler).bind(**arguments)
        except TypeError as exc:
            return {
                "ok": False,
                "error": f"Invalid arguments for {name}: {exc}",
            }

        if self.permission_manager is not None:
            decision = self.permission_manager.authorize(name, arguments)
            if decision.level is PermissionLevel.DENY:
                return {
                    "ok": False,
                    "error": f"Permission denied: {decision.reason}",
                    "permission": "denied",
                }

        try:
            result = tool.handler(**arguments)
        except ToolExecutionError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # Keep unexpected tool failures in the agent loop.
            return {"ok": False, "error": f"{name} tool failed: {exc}"}

        if not isinstance(result, dict):
            return {
                "ok": False,
                "error": f"{name} tool returned a non-object result",
            }
        return result
