"""Lifecycle hooks shared by the agent loop and tool execution path."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Any, TypeAlias


@dataclass
class UserPromptSubmit:
    """A submitted prompt before it is appended to the loop history."""

    prompt: str
    context: list[Any] = field(default_factory=list)
    rejection_reason: str | None = field(default=None, init=False)

    def add_context(self, content: str, *, role: str = "developer") -> None:
        """Inject one message immediately before the submitted user prompt."""
        if not isinstance(content, str) or not content.strip():
            raise ValueError("injected context must be a non-empty string")
        if not isinstance(role, str) or not role:
            raise ValueError("context role must be a non-empty string")
        self.context.append({"role": role, "content": content})

    def reject(self, reason: str) -> None:
        """Reject this prompt; the first rejection cannot be overwritten."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("rejection reason must be a non-empty string")
        if self.rejection_reason is None:
            self.rejection_reason = reason


@dataclass
class PreToolUse:
    """A parsed, signature-checked tool call before its handler runs."""

    tool_name: str
    arguments: Mapping[str, Any]
    call_id: str | None = None
    denial_reason: str | None = field(default=None, init=False)
    denial_result: dict[str, Any] | None = field(default=None, init=False)
    permission_level: str | None = field(default=None, init=False)
    permission_reason: str | None = field(default=None, init=False)

    def deny(
        self,
        reason: str,
        *,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        """Prevent execution and optionally choose the result returned to the model."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("denial reason must be a non-empty string")
        if self.denial_reason is not None:
            return
        self.denial_reason = reason
        self.denial_result = dict(result) if result is not None else {
            "ok": False,
            "code": "pre_tool_denied",
            "error": f"PreToolUse denied: {reason}",
            "hook": "PreToolUse",
        }


@dataclass
class PostToolUse:
    """A completed tool call whose structured result may be inspected or replaced."""

    tool_name: str
    arguments: Mapping[str, Any]
    result: dict[str, Any]
    call_id: str | None = None

    def replace_result(self, result: Mapping[str, Any]) -> None:
        """Replace the structured result that will be returned to the model."""
        self.result = dict(result)


class StopReason(str, Enum):
    """Why one invocation of :meth:`AgentLoop.run` is about to exit."""

    COMPLETED = "completed"
    ERROR = "error"


@dataclass(frozen=True)
class Stop:
    """The final lifecycle event emitted exactly once before ``run`` exits."""

    reason: StopReason
    tool_rounds: int
    output_text: str | None = None
    error: BaseException | None = None


HookEvent: TypeAlias = UserPromptSubmit | PreToolUse | PostToolUse | Stop
HookEventType: TypeAlias = (
    type[UserPromptSubmit] | type[PreToolUse] | type[PostToolUse] | type[Stop]
)
HookHandler: TypeAlias = Callable[[Any], None]

_EVENT_TYPES: tuple[HookEventType, ...] = (
    UserPromptSubmit,
    PreToolUse,
    PostToolUse,
    Stop,
)


class HookExecutionError(RuntimeError):
    """Raised when a registered hook handler fails unexpectedly."""

    def __init__(self, event_name: str, handler_name: str, cause: Exception) -> None:
        super().__init__(f"{event_name} hook {handler_name!r} failed: {cause}")
        self.event_name = event_name
        self.handler_name = handler_name
        self.cause = cause


class HookRejectedError(RuntimeError):
    """Raised when ``UserPromptSubmit`` explicitly rejects a prompt."""


class HookRegistry:
    """Register and emit the four lifecycle hook types in stable order."""

    def __init__(self, *, _execution_lock: Any | None = None) -> None:
        self._handlers: dict[HookEventType, list[HookHandler]] = {
            event_type: [] for event_type in _EVENT_TYPES
        }
        self._execution_lock = _execution_lock or RLock()

    def register(
        self,
        event_type: HookEventType,
        handler: HookHandler,
        *,
        prepend: bool = False,
    ) -> HookHandler:
        """Register a handler and return the same callable."""
        if event_type not in self._handlers:
            raise ValueError(f"Unsupported hook event: {event_type!r}")
        if not callable(handler):
            raise TypeError("hook handler must be callable")
        with self._execution_lock:
            if prepend:
                self._handlers[event_type].insert(0, handler)
            else:
                self._handlers[event_type].append(handler)
        return handler

    def handlers_for(self, event_type: HookEventType) -> tuple[HookHandler, ...]:
        """Return an immutable snapshot of handlers for introspection and tests."""
        if event_type not in self._handlers:
            raise ValueError(f"Unsupported hook event: {event_type!r}")
        with self._execution_lock:
            return tuple(self._handlers[event_type])

    def clone(self) -> HookRegistry:
        """Copy registrations while sharing the serialized execution boundary.

        Runtime composition uses this to give a child agent its own registry and
        built-in Hook state without concurrently invoking shared user handlers.
        """
        with self._execution_lock:
            cloned = HookRegistry(_execution_lock=self._execution_lock)
            cloned._handlers = {
                event_type: list(handlers)
                for event_type, handlers in self._handlers.items()
            }
        return cloned

    def emit(self, event: HookEvent) -> None:
        """Run every handler for an event, wrapping unexpected failures."""
        event_type = type(event)
        if event_type not in self._handlers:
            raise ValueError(f"Unsupported hook event: {event_type!r}")

        with self._execution_lock:
            for handler in tuple(self._handlers[event_type]):
                try:
                    handler(event)
                except HookExecutionError:
                    raise
                except Exception as exc:
                    handler_name = getattr(
                        handler,
                        "__qualname__",
                        type(handler).__name__,
                    )
                    raise HookExecutionError(
                        event_type.__name__,
                        handler_name,
                        exc,
                    ) from exc
