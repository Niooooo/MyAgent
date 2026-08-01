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
from .tooling import ToolRegistry, ToolResult

if TYPE_CHECKING:
    from .permissions import ApprovalCallback
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
        max_tool_rounds: int = 10,
        bash_tool: Callable[[str], dict[str, Any]] | None = None,
        tool_registry: ToolRegistry | None = None,
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
        self.max_tool_rounds = max_tool_rounds
        self.history: list[object] = []
        self._close_callback = close_callback
        self._close_lock = Lock()
        self._closed = False
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
            self.tool_registry = components.tool_registry
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

        self.history.extend(submitted.context)
        self.history.append({"role": "user", "content": submitted.prompt})

    def _request_response(self) -> ModelResponse:
        return self.client.responses.create(
            model=self.model,
            instructions=self.instructions,
            tools=self.tool_registry.definitions,
            input=self.history,
        )

    def _record_response(
        self,
        response: ModelResponse,
    ) -> list[FunctionCallItem]:
        # Reasoning items are protocol state, so every output item must survive.
        self.history.extend(response.output)
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
        for call in calls:
            result = self._execute_call(call)
            self.history.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps(result, ensure_ascii=False),
                }
            )

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
