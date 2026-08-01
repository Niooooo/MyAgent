"""Composition root for MyAgent's default runtime capabilities."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from .agent import DEFAULT_INSTRUCTIONS, DEFAULT_MODEL, AgentLoop, ResponsesClient
from .filesystem import WorkspaceFiles, filesystem_tools
from .hooks import HookRegistry, PreToolUse
from .permissions import (
    ApprovalCallback,
    DEFAULT_TOOL_ALLOWLIST,
    DefaultPermissionPolicy,
    PermissionHook,
    PermissionManager,
)
from .subagents import (
    DEFAULT_SUBAGENT_MAX_TASKS,
    DEFAULT_SUBAGENT_MAX_WORKERS,
    SUBAGENT_TOOL_NAMES,
    SubAgentManager,
    subagent_tools,
)
from .todo import (
    DEFAULT_TODO_REMINDER_TOOL_CALLS,
    TodoList,
    TodoReminder,
    todo_tools,
)
from .tooling import FunctionTool, ToolRegistry
from .tools import BashTool, build_bash_function_tool


_SUBAGENT_INSTRUCTIONS_SUFFIX = """

You are an isolated sub-agent. Complete only the task supplied in this user turn.
You may use the ordinary local tools that are visible to you, but you cannot create,
fork, query, collect, or manage other sub-agents. Return a concise final answer to the
parent agent; internal reasoning and tool activity are not part of that answer.
"""


def _noop() -> None:
    pass


@dataclass(frozen=True)
class AgentConfig:
    """Runtime values selected by the CLI or another application boundary."""

    model: str = DEFAULT_MODEL
    max_tool_rounds: int = 10
    bash_timeout_seconds: int = 30
    todo_reminder_tool_calls: int = DEFAULT_TODO_REMINDER_TOOL_CALLS
    subagent_max_workers: int = DEFAULT_SUBAGENT_MAX_WORKERS
    subagent_max_tasks: int = DEFAULT_SUBAGENT_MAX_TASKS
    allowed_tools: frozenset[str] | None = None


@dataclass(frozen=True)
class DefaultAgentComponents:
    """Default capabilities that belong to one agent instance."""

    tool_registry: ToolRegistry
    hooks: HookRegistry
    todo_list: TodoList
    _close_callback: Callable[[], None] = field(
        default=_noop,
        repr=False,
        compare=False,
    )

    def close(self) -> None:
        """Release background resources created by the composition root."""
        self._close_callback()


class _SerializedBashHandler:
    """Protect a caller-supplied Bash handler shared by concurrent agents."""

    def __init__(self, handler: Callable[[str], dict[str, Any]]) -> None:
        self._handler = handler
        self._lock = Lock()

    def __call__(self, command: str) -> dict[str, Any]:
        with self._lock:
            return self._handler(command)


def build_default_components(
    *,
    client: ResponsesClient | None = None,
    cwd: str | os.PathLike[str] | None = None,
    bash_tool: Callable[[str], dict[str, Any]] | None = None,
    bash_timeout_seconds: int = 30,
    allowed_tools: Iterable[str] | None = None,
    approval_callback: ApprovalCallback | None = None,
    hooks: HookRegistry | None = None,
    todo_list: TodoList | None = None,
    todo_reminder_tool_calls: int | None = None,
    model: str = DEFAULT_MODEL,
    instructions: str = DEFAULT_INSTRUCTIONS,
    max_tool_rounds: int = 10,
    subagent_max_workers: int = DEFAULT_SUBAGENT_MAX_WORKERS,
    subagent_max_tasks: int = DEFAULT_SUBAGENT_MAX_TASKS,
) -> DefaultAgentComponents:
    """Create and connect the standard tools, policies, state, and hooks."""
    if max_tool_rounds < 0:
        raise ValueError("max_tool_rounds must be non-negative")

    workspace_root = WorkspaceFiles(cwd).root
    hook_registry = hooks if hooks is not None else HookRegistry()
    child_hook_template = hook_registry.clone()
    reminder_interval = (
        DEFAULT_TODO_REMINDER_TOOL_CALLS
        if todo_reminder_tool_calls is None
        else todo_reminder_tool_calls
    )
    main_allowed_tools, child_allowed_tools = _split_allowed_tools(allowed_tools)

    if bash_tool is None:
        def make_bash_handler() -> Callable[[str], dict[str, Any]]:
            return BashTool(
                cwd=workspace_root,
                timeout_seconds=bash_timeout_seconds,
            )
    else:
        shared_bash_handler = _SerializedBashHandler(bash_tool)

        def make_bash_handler() -> Callable[[str], dict[str, Any]]:
            return shared_bash_handler

    manager: SubAgentManager | None = None
    management_tools = []
    if client is not None:
        def create_subagent() -> AgentLoop:
            child_components = _build_standard_components(
                cwd=workspace_root,
                bash_handler=make_bash_handler(),
                allowed_tools=child_allowed_tools,
                approval_callback=approval_callback,
                hooks=child_hook_template.clone(),
                todo_list=TodoList(),
                todo_reminder_tool_calls=reminder_interval,
            )
            child = AgentLoop(
                client,
                model=model,
                instructions=instructions + _SUBAGENT_INSTRUCTIONS_SUFFIX,
                max_tool_rounds=max_tool_rounds,
                tool_registry=child_components.tool_registry,
                hooks=child_components.hooks,
            )
            child.todo_list = child_components.todo_list
            return child

        manager = SubAgentManager(
            create_subagent,
            max_workers=subagent_max_workers,
            max_tasks=subagent_max_tasks,
        )
        management_tools = subagent_tools(manager)

    try:
        components = _build_standard_components(
            cwd=workspace_root,
            bash_handler=make_bash_handler(),
            allowed_tools=main_allowed_tools,
            approval_callback=approval_callback,
            hooks=hook_registry,
            todo_list=todo_list if todo_list is not None else TodoList(),
            todo_reminder_tool_calls=reminder_interval,
            additional_tools=management_tools,
        )
    except BaseException:
        if manager is not None:
            manager.close()
        raise

    if manager is None:
        return components
    return DefaultAgentComponents(
        tool_registry=components.tool_registry,
        hooks=components.hooks,
        todo_list=components.todo_list,
        _close_callback=manager.close,
    )


def _build_standard_components(
    *,
    cwd: str | os.PathLike[str],
    bash_handler: Callable[[str], dict[str, Any]],
    allowed_tools: Iterable[str] | None,
    approval_callback: ApprovalCallback | None,
    hooks: HookRegistry,
    todo_list: TodoList,
    todo_reminder_tool_calls: int,
    additional_tools: Iterable[FunctionTool] = (),
) -> DefaultAgentComponents:
    """Build one isolated ordinary capability set and its guarded registry."""
    workspace = WorkspaceFiles(cwd)
    TodoReminder(
        todo_list,
        tool_calls_without_update=todo_reminder_tool_calls,
    ).register(hooks)

    permissions = PermissionManager(
        DefaultPermissionPolicy(allowed_tools),
        approval_callback,
    )
    permission_hook = PermissionHook(permissions)
    hooks.register(PreToolUse, permission_hook, prepend=True)
    tool_registry = ToolRegistry(
        [
            build_bash_function_tool(bash_handler),
            *filesystem_tools(workspace),
            *todo_tools(todo_list),
            *additional_tools,
        ],
        tool_visibility=permission_hook.is_tool_allowed,
        hooks=hooks,
    )
    # Preserve the existing introspection attribute without making ToolRegistry
    # responsible for constructing or understanding the concrete permission type.
    tool_registry.permission_manager = permissions
    return DefaultAgentComponents(
        tool_registry=tool_registry,
        hooks=hooks,
        todo_list=todo_list,
    )


def _split_allowed_tools(
    allowed_tools: Iterable[str] | None,
) -> tuple[frozenset[str] | None, frozenset[str]]:
    if allowed_tools is None:
        return None, DEFAULT_TOOL_ALLOWLIST.difference(SUBAGENT_TOOL_NAMES)
    if isinstance(allowed_tools, str):
        raise TypeError("allowed_tools must be an iterable of tool names, not a string")
    selected = frozenset(allowed_tools)
    return selected, selected.difference(SUBAGENT_TOOL_NAMES)


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
        client=client,
        bash_timeout_seconds=selected.bash_timeout_seconds,
        allowed_tools=selected.allowed_tools,
        approval_callback=approval_callback,
        hooks=hooks,
        todo_reminder_tool_calls=selected.todo_reminder_tool_calls,
        model=selected.model,
        max_tool_rounds=selected.max_tool_rounds,
        subagent_max_workers=selected.subagent_max_workers,
        subagent_max_tasks=selected.subagent_max_tasks,
    )
    agent = AgentLoop(
        client,
        model=selected.model,
        max_tool_rounds=selected.max_tool_rounds,
        tool_registry=components.tool_registry,
        hooks=components.hooks,
        close_callback=components.close,
    )
    # Keep the state discoverable on the public AgentLoop compatibility surface.
    agent.todo_list = components.todo_list
    return agent
