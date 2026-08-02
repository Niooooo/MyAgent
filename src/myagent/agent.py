"""Core Responses API agent loop."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from os import PathLike
from threading import Lock
from typing import TYPE_CHECKING, Any, Protocol, cast

from .hooks import (
    HookExecutionError,
    HookRegistry,
    HookRejectedError,
    Stop,
    StopReason,
    UserPromptSubmit,
)
from .memory import ContextMemory, HISTORY_COMPACTION_INSTRUCTIONS
from .tooling import ToolRegistry, ToolResult

if TYPE_CHECKING:
    from .permissions import ApprovalCallback
    from .skills import SkillStore
    from .todo import TodoList


DEFAULT_INSTRUCTIONS = """You are a capable local command-line agent.
Prefer read_file, write_file, edit_file, glob, and grep for workspace file tasks.
Use bash when a shell command is genuinely needed.
Explain the final result clearly and concisely.
If a tool reports an error or refuses a command, do not claim that the command succeeded.
For multi-step work, create and maintain the global TODO list as scope or progress changes.
Do not claim all TODO items are finished until relevant checks have run and their concrete
evidence has been recorded with record_todo_verification.
"""
DEFAULT_MODEL = "gpt-5.6-sol"

InstructionsProvider = Callable[[], str]


class AgentLoopLimitError(RuntimeError):
    """Raised when the model exceeds the configured number of tool rounds."""


class ResponseOutputItem(Protocol):
    """The response item fields used by the protocol loop."""

    type: str


class FunctionCallItem(ResponseOutputItem, Protocol):
    """The additional fields present on a Responses API function call."""

    call_id: str
    name: str
    arguments: str


class ModelResponse(Protocol):
    """The response fields consumed by :class:`AgentLoop`."""

    output: Sequence[ResponseOutputItem]
    output_text: str


class ResponsesEndpoint(Protocol):
    def create(
        self,
        *,
        model: str,
        instructions: str,
        tools: list[dict[str, Any]],
        input: list[object],
    ) -> ModelResponse: ...


class ResponsesClient(Protocol):
    responses: ResponsesEndpoint


class AgentLoop:
    """Run a stateful OpenAI Responses API tool-calling loop."""

    def __init__(
        self,
        client: ResponsesClient,
        *,
        model: str = DEFAULT_MODEL,
        instructions: str = DEFAULT_INSTRUCTIONS,
        instructions_provider: InstructionsProvider | None = None,
        max_tool_rounds: int = 10,
        bash_tool: Callable[[str], dict[str, Any]] | None = None,
        tool_registry: ToolRegistry | None = None,
        context_memory: ContextMemory | None = None,
        workspace_root: str | PathLike[str] | None = None,
        allowed_tools: Iterable[str] | None = None,
        approval_callback: ApprovalCallback | None = None,
        hooks: HookRegistry | None = None,
        todo_list: TodoList | None = None,
        todo_reminder_tool_calls: int | None = None,
        close_callback: Callable[[], None] | None = None,
    ) -> None:
        if max_tool_rounds < 0:
            raise ValueError("max_tool_rounds must be non-negative")
        if instructions_provider is not None and not callable(instructions_provider):
            raise TypeError("instructions_provider must be callable")
        if context_memory is not None and not isinstance(
            context_memory,
            ContextMemory,
        ):
            raise TypeError("context_memory must be a ContextMemory")
        if tool_registry is None and context_memory is not None:
            raise ValueError("context_memory requires an explicit tool_registry")
        if tool_registry is not None and any(
            value is not None
            for value in (
                bash_tool,
                workspace_root,
                allowed_tools,
                approval_callback,
                todo_list,
                todo_reminder_tool_calls,
            )
        ):
            raise ValueError(
                "Pass either tool_registry or default-tool configuration, not both"
            )

        self.client = client
        self.model = model
        self.instructions = instructions
        self.instructions_provider = instructions_provider
        self.max_tool_rounds = max_tool_rounds
        self.history: list[object] = []
        self._close_callback = close_callback
        self._close_lock = Lock()
        self._closed = False
        self.skill_store: SkillStore | None = None
        self.context_memory: ContextMemory | None = context_memory
        if tool_registry is not None:
            if hooks is not None and tool_registry.hooks is not hooks:
                raise ValueError(
                    "AgentLoop and tool_registry must share the same HookRegistry"
                )
            self.tool_registry = tool_registry
            self.todo_list = None
        else:
            # The broad constructor is retained for compatibility. New application
            # code should assemble defaults through composition.create_default_agent.
            from .composition import build_default_components

            components = build_default_components(
                client=client,
                cwd=workspace_root,
                bash_tool=bash_tool,
                allowed_tools=None if allowed_tools is None else set(allowed_tools),
                approval_callback=approval_callback,
                hooks=hooks,
                todo_list=todo_list,
                todo_reminder_tool_calls=todo_reminder_tool_calls,
                model=model,
                instructions=instructions,
                max_tool_rounds=max_tool_rounds,
            )
            self.todo_list = components.todo_list
            self.skill_store = components.skill_store
            self.context_memory = components.context_memory
            self.tool_registry = components.tool_registry
            self.instructions_provider = _combine_instructions_providers(
                self.instructions_provider,
                components.instructions_provider,
            )
            if self._close_callback is None:
                self._close_callback = components.close
        self.hooks = self.tool_registry.hooks

    def run(self, user_input: str) -> str:
        """Process one user turn and return the model's final text."""
        tool_rounds = 0
        output_text: str | None = None
        failure: BaseException | None = None

        try:
            self._submit_user_input(user_input)

            while True:
                response = self._request_response()
                calls = self._record_response(response)

                if not calls:
                    output_text = self._require_output_text(response)
                    return output_text

                self._ensure_tool_round_available(tool_rounds)
                self._execute_calls(calls)
                tool_rounds += 1
        except BaseException as exc:
            failure = exc
            raise
        finally:
            stop = Stop(
                reason=(
                    StopReason.COMPLETED if failure is None else StopReason.ERROR
                ),
                tool_rounds=tool_rounds,
                output_text=output_text,
                error=failure,
            )
            self._emit_stop(stop, failure)

    def reset(self) -> None:
        """Clear the in-memory conversation history."""
        self.history.clear()
        if self.context_memory is not None:
            self.context_memory.reset()

    def close(self) -> None:
        """Release resources attached by the runtime composition root."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            callback = self._close_callback
        if callback is not None:
            callback()

    def _submit_user_input(self, user_input: str) -> None:
        if not isinstance(user_input, str):
            raise TypeError("user_input must be a string")
        submitted = UserPromptSubmit(user_input)
        self.hooks.emit(submitted)
        if submitted.rejection_reason is not None:
            raise HookRejectedError(
                f"UserPromptSubmit rejected: {submitted.rejection_reason}"
            )
        if not isinstance(submitted.prompt, str) or not submitted.prompt.strip():
            raise ValueError("user_input must not be empty")

        items = [
            *submitted.context,
            {"role": "user", "content": submitted.prompt},
        ]
        self.history.extend(items)
        if self.context_memory is not None:
            self.context_memory.record_user_turn(items)

    def _request_response(self) -> ModelResponse:
        if self.context_memory is not None:
            self.context_memory.prepare_request(
                self.history,
                self._request_history_summary,
            )
        response = self.client.responses.create(
            model=self.model,
            instructions=self._effective_instructions(),
            tools=self.tool_registry.definitions,
            input=self.history,
        )
        if self.context_memory is not None:
            self.context_memory.mark_request_succeeded()
        return response

    def _request_history_summary(self, source: str, max_chars: int) -> str:
        response = self.client.responses.create(
            model=self.model,
            instructions=HISTORY_COMPACTION_INSTRUCTIONS,
            tools=[],
            input=[
                {
                    "role": "user",
                    "content": (
                        f"Compress the following historical records to at most "
                        f"{max_chars} characters.\n\n{source}"
                    ),
                }
            ],
        )
        summary = response.output_text.strip()
        if not summary:
            raise RuntimeError("The history summarizer returned empty text")
        return summary

    def _effective_instructions(self) -> str:
        provider = self.instructions_provider
        if provider is None:
            return self.instructions
        dynamic_instructions = provider()
        if not isinstance(dynamic_instructions, str):
            raise TypeError("instructions_provider must return a string")
        if not dynamic_instructions.strip():
            return self.instructions
        return f"{self.instructions}\n\n{dynamic_instructions}"

    def _record_response(
        self,
        response: ModelResponse,
    ) -> list[FunctionCallItem]:
        # Reasoning items are protocol state, so every output item must survive.
        self.history.extend(response.output)
        if self.context_memory is not None:
            self.context_memory.record_response(response.output)
        return [
            cast(FunctionCallItem, item)
            for item in response.output
            if item.type == "function_call"
        ]

    @staticmethod
    def _require_output_text(response: ModelResponse) -> str:
        output_text = response.output_text.strip()
        if not output_text:
            raise RuntimeError("The model returned neither tool calls nor text")
        return output_text

    def _ensure_tool_round_available(self, tool_rounds: int) -> None:
        if tool_rounds >= self.max_tool_rounds:
            raise AgentLoopLimitError(
                f"Agent exceeded {self.max_tool_rounds} tool rounds"
            )

    def _execute_calls(self, calls: Sequence[FunctionCallItem]) -> None:
        # Every handler and PostToolUse runs against the complete structured result
        # before context memory sees serialized output.
        results = [(call, self._execute_call(call)) for call in calls]
        outputs: list[object] = []
        for call, result in results:
            serialized = json.dumps(result, ensure_ascii=False)
            if self.context_memory is not None:
                serialized = self.context_memory.prepare_tool_output(serialized)
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": serialized,
                }
            )
        self.history.extend(outputs)
        if self.context_memory is not None:
            self.context_memory.record_tool_outputs(outputs)

    def _execute_call(self, call: FunctionCallItem) -> ToolResult:
        return self.tool_registry.execute(
            call.name,
            call.arguments,
            call_id=call.call_id,
        )

    def _emit_stop(self, event: Stop, failure: BaseException | None) -> None:
        """Run cleanup hooks without hiding the error that stopped the loop."""
        try:
            self.hooks.emit(event)
        except HookExecutionError as exc:
            if failure is None:
                raise
            if hasattr(failure, "add_note"):
                failure.add_note(f"A Stop hook also failed: {exc}")


def _combine_instructions_providers(
    *providers: InstructionsProvider | None,
) -> InstructionsProvider | None:
    selected = tuple(provider for provider in providers if provider is not None)
    if not selected:
        return None
    if len(selected) == 1:
        return selected[0]

    def combined() -> str:
        fragments: list[str] = []
        for provider in selected:
            fragment = provider()
            if not isinstance(fragment, str):
                raise TypeError("instructions_provider must return a string")
            if fragment.strip():
                fragments.append(fragment)
        return "\n\n".join(fragments)

    return combined
