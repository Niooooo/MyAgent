"""Composition root for MyAgent's default runtime capabilities."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .agent import DEFAULT_MODEL, AgentLoop, ResponsesClient
from .filesystem import WorkspaceFiles, filesystem_tools
from .hooks import HookRegistry, PreToolUse
from .permissions import (
    ApprovalCallback,
    DefaultPermissionPolicy,
    PermissionHook,
    PermissionManager,
)
from .todo import (
    DEFAULT_TODO_REMINDER_TOOL_CALLS,
    TodoList,
    TodoReminder,
    todo_tools,
)
from .tooling import ToolRegistry
from .tools import BashTool, build_bash_function_tool


@dataclass(frozen=True)
class AgentConfig:
    """Runtime values selected by the CLI or another application boundary."""

    model: str = DEFAULT_MODEL
    max_tool_rounds: int = 10
    bash_timeout_seconds: int = 30
    todo_reminder_tool_calls: int = DEFAULT_TODO_REMINDER_TOOL_CALLS


@dataclass(frozen=True)
class DefaultAgentComponents:
    """Default capabilities that belong to one agent instance."""

    tool_registry: ToolRegistry
    hooks: HookRegistry
    todo_list: TodoList


def build_default_components(
    *,
    cwd: str | os.PathLike[str] | None = None,
    bash_tool: Callable[[str], dict[str, Any]] | None = None,
    allowed_tools: Iterable[str] | None = None,
    approval_callback: ApprovalCallback | None = None,
    hooks: HookRegistry | None = None,
    todo_list: TodoList | None = None,
    todo_reminder_tool_calls: int | None = None,
) -> DefaultAgentComponents:
    """Create and connect the standard tools, policies, state, and hooks."""
    workspace = WorkspaceFiles(cwd)
    bash_handler = bash_tool if bash_tool is not None else BashTool(cwd=workspace.root)
    todo_state = todo_list if todo_list is not None else TodoList()
    hook_registry = hooks if hooks is not None else HookRegistry()
    reminder_interval = (
        DEFAULT_TODO_REMINDER_TOOL_CALLS
        if todo_reminder_tool_calls is None
        else todo_reminder_tool_calls
    )
    TodoReminder(
        todo_state,
        tool_calls_without_update=reminder_interval,
    ).register(hook_registry)

    permissions = PermissionManager(
        DefaultPermissionPolicy(allowed_tools),
        approval_callback,
    )
    permission_hook = PermissionHook(permissions)
    hook_registry.register(PreToolUse, permission_hook, prepend=True)
    tool_registry = ToolRegistry(
        [
            build_bash_function_tool(bash_handler),
            *filesystem_tools(workspace),
            *todo_tools(todo_state),
        ],
        tool_visibility=permission_hook.is_tool_allowed,
        hooks=hook_registry,
    )
    # Preserve the existing introspection attribute without making ToolRegistry
    # responsible for constructing or understanding the concrete permission type.
    tool_registry.permission_manager = permissions
    return DefaultAgentComponents(
        tool_registry=tool_registry,
        hooks=hook_registry,
        todo_list=todo_state,
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
    """Compatibility-friendly shortcut for callers that only need the registry."""
    return build_default_components(
        cwd=cwd,
        bash_tool=bash_tool,
        allowed_tools=allowed_tools,
        approval_callback=approval_callback,
        hooks=hooks,
        todo_list=todo_list,
        todo_reminder_tool_calls=todo_reminder_tool_calls,
    ).tool_registry


def create_default_agent(
    client: ResponsesClient,
    *,
    config: AgentConfig | None = None,
    approval_callback: ApprovalCallback | None = None,
    hooks: HookRegistry | None = None,
) -> AgentLoop:
    """Create one fully connected default agent for an application boundary."""
    selected = config if config is not None else AgentConfig()
    components = build_default_components(
        bash_tool=BashTool(timeout_seconds=selected.bash_timeout_seconds),
        approval_callback=approval_callback,
        hooks=hooks,
        todo_reminder_tool_calls=selected.todo_reminder_tool_calls,
    )
    agent = AgentLoop(
        client,
        model=selected.model,
        max_tool_rounds=selected.max_tool_rounds,
        tool_registry=components.tool_registry,
        hooks=components.hooks,
    )
    # Keep the state discoverable on the public AgentLoop compatibility surface.
    agent.todo_list = components.todo_list
    return agent
