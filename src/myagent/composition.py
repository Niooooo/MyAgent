"""Composition root for MyAgent's default runtime capabilities."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from .agent import (
    DEFAULT_INSTRUCTIONS,
    DEFAULT_MODEL,
    FALLBACK_MODEL,
    AgentLoop,
    InstructionsProvider,
    ResponsesClient,
)
from .filesystem import WorkspaceFiles, filesystem_tools
from .hooks import HookRegistry, PreToolUse
from .long_term_memory import LongTermMemoryStore, long_term_memory_tools
from .memory import (
    ContextMemory,
    MemoryConfig,
    ToolResultStore,
    memory_tools,
)
from .agent_team import (
    AGENT_TEAM_TOOL_NAMES,
    CREATE_TEAMMATE_TOOL,
    MAIN_AGENT_NAME,
    RUN_TEAMMATE_TOOL,
    AgentTeamManager,
    main_team_tools,
    team_message_tool,
)
from .permissions import (
    ApprovalCallback,
    ApprovalRequest,
    DEFAULT_TOOL_ALLOWLIST,
    DefaultPermissionPolicy,
    PermissionHook,
    PermissionManager,
)
from .skills import LOAD_SKILL_TOOL, SkillStore, render_skill_catalog, skill_tools
from .subagents import (
    DEFAULT_SUBAGENT_MAX_TASKS,
    DEFAULT_SUBAGENT_MAX_WORKERS,
    SUBAGENT_TOOL_NAMES,
    SubAgentManager,
    subagent_tools,
)
from .runtime_inbox import InboxReader
from .scheduled_tasks import (
    SCHEDULED_TASK_TOOL_NAMES,
    ScheduledTaskRuntime,
    scheduled_task_tools,
)
from .tasks import TaskStore, task_tools
from .todo import (
    DEFAULT_TODO_REMINDER_TOOL_CALLS,
    TodoList,
    TodoReminder,
    todo_tools,
)
from .tooling import FunctionTool, ToolRegistry
from .tools import (
    BackgroundBashRunner,
    BashTool,
    build_background_bash_function_tool,
    build_bash_function_tool,
)


_SUBAGENT_INSTRUCTIONS_SUFFIX = """

You are an isolated sub-agent. Complete only the task supplied in this user turn.
You may use the ordinary local tools that are visible to you, but you cannot create,
fork, query, collect, or manage other sub-agents. Return a concise final answer to the
parent agent; internal reasoning and tool activity are not part of that answer.
"""

_TEAMMATE_INSTRUCTIONS_SUFFIX = """

You are the persistent Agent Team member named {name}. Your role is: {role}
Retain useful context across assigned turns. You may message another Team member with
send_team_message, whose sender identity is bound by the runtime. You cannot create or
run teammates or manage isolated sub-agents.
"""


def _noop() -> None:
    pass


@dataclass(frozen=True)
class AgentConfig:
    """Runtime values selected by the CLI or another application boundary."""

    model: str = DEFAULT_MODEL
    fallback_model: str = FALLBACK_MODEL
    max_tool_rounds: int = 10
    bash_timeout_seconds: int = 30
    todo_reminder_tool_calls: int = DEFAULT_TODO_REMINDER_TOOL_CALLS
    subagent_max_workers: int = DEFAULT_SUBAGENT_MAX_WORKERS
    subagent_max_tasks: int = DEFAULT_SUBAGENT_MAX_TASKS
    allowed_tools: frozenset[str] | None = None
    memory: MemoryConfig = field(default_factory=MemoryConfig)

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must be a non-empty string")
        if not isinstance(self.fallback_model, str) or not self.fallback_model.strip():
            raise ValueError("fallback_model must be a non-empty string")
        if not isinstance(self.memory, MemoryConfig):
            raise TypeError("memory must be a MemoryConfig")


@dataclass(frozen=True)
class DefaultAgentComponents:
    """Default capabilities that belong to one agent instance."""

    tool_registry: ToolRegistry
    hooks: HookRegistry
    todo_list: TodoList
    task_store: TaskStore | None = None
    skill_store: SkillStore | None = None
    long_term_memory_store: LongTermMemoryStore | None = None
    instructions_provider: InstructionsProvider | None = None
    context_memory: ContextMemory | None = None
    tool_result_store: ToolResultStore | None = None
    background_bash_runner: BackgroundBashRunner | None = None
    scheduled_task_runtime: ScheduledTaskRuntime | None = None
    agent_team_manager: AgentTeamManager | None = None
    inbox_reader: InboxReader | None = None
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
    fallback_model: str = FALLBACK_MODEL,
    instructions: str = DEFAULT_INSTRUCTIONS,
    max_tool_rounds: int = 10,
    subagent_max_workers: int = DEFAULT_SUBAGENT_MAX_WORKERS,
    subagent_max_tasks: int = DEFAULT_SUBAGENT_MAX_TASKS,
    skill_store: SkillStore | None = None,
    long_term_memory_store: LongTermMemoryStore | None = None,
    memory_config: MemoryConfig | None = None,
    tool_result_store: ToolResultStore | None = None,
    task_store: TaskStore | None = None,
) -> DefaultAgentComponents:
    """Create and connect the standard tools, policies, state, and hooks."""
    if max_tool_rounds < 0:
        raise ValueError("max_tool_rounds must be non-negative")

    workspace_root = WorkspaceFiles(cwd).root
    if skill_store is not None and skill_store.workspace_root != workspace_root:
        raise ValueError("skill_store must use the configured workspace root")
    if (
        long_term_memory_store is not None
        and long_term_memory_store.workspace_root != workspace_root
    ):
        raise ValueError(
            "long_term_memory_store must use the configured workspace root"
        )
    if (
        tool_result_store is not None
        and tool_result_store.workspace_root != workspace_root
    ):
        raise ValueError("tool_result_store must use the configured workspace root")
    if task_store is not None and task_store.workspace_root != workspace_root:
        raise ValueError("task_store must use the configured workspace root")
    selected_memory_config = (
        memory_config if memory_config is not None else MemoryConfig()
    )
    if not isinstance(selected_memory_config, MemoryConfig):
        raise TypeError("memory_config must be a MemoryConfig")
    shared_skill_store = skill_store or SkillStore(workspace_root)
    shared_long_term_memory_store = (
        long_term_memory_store or LongTermMemoryStore(workspace_root)
    )
    shared_tool_result_store = tool_result_store or ToolResultStore(workspace_root)
    shared_task_store = task_store or TaskStore(workspace_root)
    hook_registry = hooks if hooks is not None else HookRegistry()
    child_hook_template = hook_registry.clone()
    reminder_interval = (
        DEFAULT_TODO_REMINDER_TOOL_CALLS
        if todo_reminder_tool_calls is None
        else todo_reminder_tool_calls
    )
    main_allowed_tools, child_allowed_tools, teammate_allowed_tools = (
        _split_allowed_tools(allowed_tools)
    )

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

    main_bash_handler = make_bash_handler()
    background_bash_runner = (
        BackgroundBashRunner(main_bash_handler) if client is not None else None
    )

    manager: SubAgentManager | None = None
    team_manager: AgentTeamManager | None = None
    management_tools = []
    scheduled_task_runtime: ScheduledTaskRuntime | None = None
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
                skill_store=shared_skill_store,
                long_term_memory_store=shared_long_term_memory_store,
                memory_config=selected_memory_config,
                tool_result_store=shared_tool_result_store,
                task_store=shared_task_store,
            )
            try:
                child = AgentLoop(
                    client,
                    model=model,
                    fallback_model=fallback_model,
                    instructions=instructions + _SUBAGENT_INSTRUCTIONS_SUFFIX,
                    max_tool_rounds=max_tool_rounds,
                    tool_registry=child_components.tool_registry,
                    hooks=child_components.hooks,
                    instructions_provider=child_components.instructions_provider,
                    context_memory=child_components.context_memory,
                    close_callback=child_components.close,
                )
            except BaseException:
                child_components.close()
                raise
            child.todo_list = child_components.todo_list
            child.skill_store = child_components.skill_store
            child.long_term_memory_store = child_components.long_term_memory_store
            child.task_store = child_components.task_store
            return child

        manager = SubAgentManager(
            create_subagent,
            max_workers=subagent_max_workers,
            max_tasks=subagent_max_tasks,
        )
        management_tools = subagent_tools(manager)

        def create_teammate_agent(
            name: str,
            role: str,
            inbox_reader: InboxReader,
        ) -> AgentLoop:
            if team_manager is None:
                raise RuntimeError("Agent Team manager is unavailable")

            def teammate_approval(request: ApprovalRequest) -> bool:
                if approval_callback is None:
                    return False
                return approval_callback(
                    ApprovalRequest(
                        request.tool_name,
                        request.arguments,
                        f"Agent Team member {name}: {request.reason}",
                    )
                )

            teammate_components = _build_standard_components(
                cwd=workspace_root,
                bash_handler=make_bash_handler(),
                allowed_tools=teammate_allowed_tools,
                approval_callback=(
                    teammate_approval if approval_callback is not None else None
                ),
                hooks=child_hook_template.clone(),
                todo_list=TodoList(),
                todo_reminder_tool_calls=reminder_interval,
                skill_store=shared_skill_store,
                long_term_memory_store=shared_long_term_memory_store,
                memory_config=selected_memory_config,
                tool_result_store=shared_tool_result_store,
                task_store=shared_task_store,
                additional_tools=[team_message_tool(team_manager, name)],
            )
            try:
                teammate = AgentLoop(
                    client,
                    model=model,
                    fallback_model=fallback_model,
                    instructions=(
                        instructions
                        + _TEAMMATE_INSTRUCTIONS_SUFFIX.format(name=name, role=role)
                    ),
                    max_tool_rounds=max_tool_rounds,
                    tool_registry=teammate_components.tool_registry,
                    hooks=teammate_components.hooks,
                    instructions_provider=teammate_components.instructions_provider,
                    context_memory=teammate_components.context_memory,
                    inbox_reader=inbox_reader,
                    close_callback=teammate_components.close,
                )
            except BaseException:
                teammate_components.close()
                raise
            teammate.todo_list = teammate_components.todo_list
            teammate.skill_store = teammate_components.skill_store
            teammate.long_term_memory_store = teammate_components.long_term_memory_store
            teammate.task_store = teammate_components.task_store
            return teammate

        try:
            team_manager = AgentTeamManager(workspace_root, create_teammate_agent)
        except BaseException:
            try:
                if background_bash_runner is not None:
                    background_bash_runner.close()
            finally:
                manager.close()
            raise
        management_tools.extend(main_team_tools(team_manager))

    try:
        components = _build_standard_components(
            cwd=workspace_root,
            bash_handler=main_bash_handler,
            allowed_tools=main_allowed_tools,
            approval_callback=approval_callback,
            hooks=hook_registry,
            todo_list=todo_list if todo_list is not None else TodoList(),
            todo_reminder_tool_calls=reminder_interval,
            skill_store=shared_skill_store,
            long_term_memory_store=shared_long_term_memory_store,
            memory_config=selected_memory_config,
            tool_result_store=shared_tool_result_store,
            task_store=shared_task_store,
            additional_tools=[
                *management_tools,
                *(
                    [build_background_bash_function_tool(background_bash_runner)]
                    if background_bash_runner is not None
                    else []
                ),
            ],
        )
    except BaseException:
        try:
            if scheduled_task_runtime is not None:
                scheduled_task_runtime.close()
        finally:
            try:
                if background_bash_runner is not None:
                    background_bash_runner.close()
            finally:
                if manager is not None:
                    manager.close()
                if team_manager is not None:
                    team_manager.close()
        raise

    if client is not None:
        try:
            schedulable_tool_names = {
                definition["name"]
                for definition in components.tool_registry.definitions
            }.difference(SCHEDULED_TASK_TOOL_NAMES)

            def execute_scheduled_tool(tool_name: str, tool_arguments: str) -> object:
                return components.tool_registry.execute(
                    tool_name,
                    tool_arguments,
                    call_id=None,
                )

            scheduled_task_runtime = ScheduledTaskRuntime(
                execute_scheduled_tool,
                schedulable_tool_names,
            )
            for tool in scheduled_task_tools(scheduled_task_runtime):
                components.tool_registry.register(tool)
            scheduled_task_runtime.start()
        except BaseException:
            try:
                if scheduled_task_runtime is not None:
                    scheduled_task_runtime.close()
            finally:
                try:
                    if background_bash_runner is not None:
                        background_bash_runner.close()
                finally:
                    if manager is not None:
                        manager.close()
                    if team_manager is not None:
                        team_manager.close()
            raise

    if (
        manager is None
        and background_bash_runner is None
        and scheduled_task_runtime is None
        and team_manager is None
    ):
        return components

    def close_runtime() -> None:
        try:
            if scheduled_task_runtime is not None:
                scheduled_task_runtime.close()
        finally:
            try:
                if background_bash_runner is not None:
                    background_bash_runner.close()
            finally:
                if manager is not None:
                    manager.close()
                if team_manager is not None:
                    team_manager.close()

    return DefaultAgentComponents(
        tool_registry=components.tool_registry,
        hooks=components.hooks,
        todo_list=components.todo_list,
        task_store=components.task_store,
        skill_store=components.skill_store,
        long_term_memory_store=components.long_term_memory_store,
        instructions_provider=components.instructions_provider,
        context_memory=components.context_memory,
        tool_result_store=components.tool_result_store,
        background_bash_runner=background_bash_runner,
        scheduled_task_runtime=scheduled_task_runtime,
        agent_team_manager=team_manager,
        inbox_reader=(
            team_manager.inbox_reader(MAIN_AGENT_NAME)
            if team_manager is not None
            else None
        ),
        _close_callback=close_runtime,
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
    skill_store: SkillStore,
    long_term_memory_store: LongTermMemoryStore,
    memory_config: MemoryConfig,
    tool_result_store: ToolResultStore,
    task_store: TaskStore,
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
            *task_tools(task_store),
            *skill_tools(skill_store),
            *long_term_memory_tools(long_term_memory_store),
            *memory_tools(
                tool_result_store,
                max_chars=memory_config.load_memory_max_chars,
            ),
            *additional_tools,
        ],
        tool_visibility=permission_hook.is_tool_allowed,
        hooks=hooks,
    )
    # Preserve the existing introspection attribute without making ToolRegistry
    # responsible for constructing or understanding the concrete permission type.
    tool_registry.permission_manager = permissions
    instructions_provider = None
    if permission_hook.is_tool_allowed(LOAD_SKILL_TOOL):
        instructions_provider = _skill_catalog_provider(skill_store)
    return DefaultAgentComponents(
        tool_registry=tool_registry,
        hooks=hooks,
        todo_list=todo_list,
        task_store=task_store,
        skill_store=skill_store,
        long_term_memory_store=long_term_memory_store,
        instructions_provider=instructions_provider,
        context_memory=ContextMemory(tool_result_store, memory_config),
        tool_result_store=tool_result_store,
    )


def _skill_catalog_provider(store: SkillStore) -> InstructionsProvider:
    def provide() -> str:
        return render_skill_catalog(store.catalog())

    return provide


def _split_allowed_tools(
    allowed_tools: Iterable[str] | None,
) -> tuple[frozenset[str] | None, frozenset[str], frozenset[str]]:
    if allowed_tools is None:
        return (
            None,
            DEFAULT_TOOL_ALLOWLIST.difference(
                SUBAGENT_TOOL_NAMES
                | SCHEDULED_TASK_TOOL_NAMES
                | AGENT_TEAM_TOOL_NAMES
            ),
            DEFAULT_TOOL_ALLOWLIST.difference(
                SUBAGENT_TOOL_NAMES
                | SCHEDULED_TASK_TOOL_NAMES
                | {CREATE_TEAMMATE_TOOL, RUN_TEAMMATE_TOOL}
            ),
        )
    if isinstance(allowed_tools, str):
        raise TypeError("allowed_tools must be an iterable of tool names, not a string")
    selected = frozenset(allowed_tools)
    return selected, selected.difference(
        SUBAGENT_TOOL_NAMES | SCHEDULED_TASK_TOOL_NAMES | AGENT_TEAM_TOOL_NAMES
    ), selected.difference(
        SUBAGENT_TOOL_NAMES
        | SCHEDULED_TASK_TOOL_NAMES
        | {CREATE_TEAMMATE_TOOL, RUN_TEAMMATE_TOOL}
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
        client=client,
        bash_timeout_seconds=selected.bash_timeout_seconds,
        allowed_tools=selected.allowed_tools,
        approval_callback=approval_callback,
        hooks=hooks,
        todo_reminder_tool_calls=selected.todo_reminder_tool_calls,
        model=selected.model,
        fallback_model=selected.fallback_model,
        max_tool_rounds=selected.max_tool_rounds,
        subagent_max_workers=selected.subagent_max_workers,
        subagent_max_tasks=selected.subagent_max_tasks,
        memory_config=selected.memory,
    )
    try:
        agent = AgentLoop(
            client,
            model=selected.model,
            fallback_model=selected.fallback_model,
            max_tool_rounds=selected.max_tool_rounds,
            tool_registry=components.tool_registry,
            hooks=components.hooks,
            instructions_provider=components.instructions_provider,
            context_memory=components.context_memory,
            background_bash_runner=components.background_bash_runner,
            scheduled_task_runtime=components.scheduled_task_runtime,
            inbox_reader=components.inbox_reader,
            close_callback=components.close,
        )
    except BaseException:
        components.close()
        raise
    # Keep the state discoverable on the public AgentLoop compatibility surface.
    agent.todo_list = components.todo_list
    agent.skill_store = components.skill_store
    agent.long_term_memory_store = components.long_term_memory_store
    agent.task_store = components.task_store
    agent.scheduled_task_runtime = components.scheduled_task_runtime
    agent.agent_team_manager = components.agent_team_manager
    return agent
