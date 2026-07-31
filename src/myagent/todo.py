"""Global in-memory TODO state, tools, and model reminders."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .hooks import HookRegistry, PostToolUse, UserPromptSubmit
from .tooling import FunctionTool, ToolExecutionError


DEFAULT_TODO_REMINDER_TOOL_CALLS = 4
MAX_TODO_ITEMS = 50
MAX_TODO_CONTENT_CHARS = 500
MAX_VERIFICATION_EVIDENCE_CHARS = 2_000

UPDATE_TODO_LIST_TOOL = "update_todo_list"
GET_TODO_LIST_TOOL = "get_todo_list"
RECORD_TODO_VERIFICATION_TOOL = "record_todo_verification"
TODO_TOOL_NAMES = frozenset(
    {
        UPDATE_TODO_LIST_TOOL,
        GET_TODO_LIST_TOOL,
        RECORD_TODO_VERIFICATION_TOOL,
    }
)


class TodoStatus(str, Enum):
    """The only valid lifecycle states for a TODO item."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclass(frozen=True)
class TodoItem:
    """One immutable item in the global TODO list."""

    content: str
    status: TodoStatus

    def as_dict(self) -> dict[str, str]:
        return {"content": self.content, "status": self.status.value}


class TodoList:
    """Store one task list shared across all turns of an agent instance."""

    def __init__(self) -> None:
        self._items: tuple[TodoItem, ...] = ()
        self._version = 0
        self._verified_version: int | None = None
        self._verification_evidence: str | None = None

    @property
    def items(self) -> tuple[TodoItem, ...]:
        return self._items

    @property
    def all_completed(self) -> bool:
        return bool(self._items) and all(
            item.status is TodoStatus.COMPLETED for item in self._items
        )

    @property
    def verified(self) -> bool:
        return self.all_completed and self._verified_version == self._version

    def replace(self, items: object) -> dict[str, Any]:
        """Create or replace the complete list and invalidate stale verification."""
        normalized = self._normalize_items(items)
        changed = normalized != self._items
        if changed:
            self._items = normalized
            self._version += 1
            self._verified_version = None
            self._verification_evidence = None

        return {"ok": True, "changed": changed, **self.snapshot()}

    def record_verification(self, evidence: object) -> dict[str, Any]:
        """Record concrete verification evidence for the current list version."""
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("verification evidence must be a non-empty string")
        evidence = evidence.strip()
        if len(evidence) > MAX_VERIFICATION_EVIDENCE_CHARS:
            raise ValueError(
                "verification evidence exceeds "
                f"{MAX_VERIFICATION_EVIDENCE_CHARS} characters"
            )
        if not self._items:
            raise ValueError("cannot verify an empty TODO list")
        if not self.all_completed:
            raise ValueError(
                "cannot verify the TODO list before every item is completed"
            )

        changed = (
            self._verified_version != self._version
            or self._verification_evidence != evidence
        )
        self._verified_version = self._version
        self._verification_evidence = evidence
        return {"ok": True, "changed": changed, **self.snapshot()}

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable view of the current list."""
        return {
            "todo_list": [item.as_dict() for item in self._items],
            "version": self._version,
            "all_completed": self.all_completed,
            "verified": self.verified,
            "verification_evidence": (
                self._verification_evidence if self.verified else None
            ),
        }

    @staticmethod
    def _normalize_items(items: object) -> tuple[TodoItem, ...]:
        if not isinstance(items, list):
            raise ValueError("items must be a JSON array")
        if not items:
            raise ValueError("a TODO list must contain at least one item")
        if len(items) > MAX_TODO_ITEMS:
            raise ValueError(f"a TODO list may contain at most {MAX_TODO_ITEMS} items")

        normalized: list[TodoItem] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"items[{index}] must be an object")
            if set(item) != {"content", "status"}:
                raise ValueError(
                    f"items[{index}] must contain exactly content and status"
                )

            content = item["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError(f"items[{index}].content must be a non-empty string")
            content = content.strip()
            if len(content) > MAX_TODO_CONTENT_CHARS:
                raise ValueError(
                    f"items[{index}].content exceeds "
                    f"{MAX_TODO_CONTENT_CHARS} characters"
                )

            status = item["status"]
            try:
                normalized_status = TodoStatus(status)
            except (TypeError, ValueError) as exc:
                choices = ", ".join(member.value for member in TodoStatus)
                raise ValueError(
                    f"items[{index}].status must be one of: {choices}"
                ) from exc
            normalized.append(TodoItem(content, normalized_status))

        return tuple(normalized)


class TodoReminder:
    """Inject TODO state after drift or before unverified completion claims."""

    def __init__(
        self,
        todo_list: TodoList,
        *,
        tool_calls_without_update: int = DEFAULT_TODO_REMINDER_TOOL_CALLS,
    ) -> None:
        if not isinstance(tool_calls_without_update, int) or isinstance(
            tool_calls_without_update, bool
        ):
            raise TypeError("tool_calls_without_update must be an integer")
        if tool_calls_without_update <= 0:
            raise ValueError("tool_calls_without_update must be positive")
        self.todo_list = todo_list
        self.tool_calls_without_update = tool_calls_without_update
        self._tool_calls_since_update = 0
        self._last_drift_reminder_at = 0

    def register(self, hooks: HookRegistry) -> None:
        """Attach both reminder entry points to a shared hook registry."""
        hooks.register(UserPromptSubmit, self.on_user_prompt_submit)
        hooks.register(PostToolUse, self.on_post_tool_use)

    def on_user_prompt_submit(self, event: UserPromptSubmit) -> None:
        """Carry unresolved TODO warnings across user turns."""
        reminder = self._current_reminder()
        if reminder is None:
            return
        event.add_context(
            "TODO LIST CONTEXT:\n"
            + json.dumps(reminder, ensure_ascii=False, indent=2)
        )
        if reminder["reason"] == "todo_list_stale":
            self._last_drift_reminder_at = self._tool_calls_since_update

    def on_post_tool_use(self, event: PostToolUse) -> None:
        """Track tool activity and append reminders to function-call output."""
        changed = bool(
            event.tool_name in {UPDATE_TODO_LIST_TOOL, RECORD_TODO_VERIFICATION_TOOL}
            and event.result.get("ok") is True
            and event.result.get("changed") is True
        )
        if changed:
            self._tool_calls_since_update = 0
            self._last_drift_reminder_at = 0
        elif self.todo_list.items:
            self._tool_calls_since_update += 1
        else:
            self._tool_calls_since_update = 0
            self._last_drift_reminder_at = 0

        reminder = self._current_reminder()
        if reminder is None:
            return
        event.replace_result({**event.result, "todo_reminder": reminder})
        if reminder["reason"] == "todo_list_stale":
            self._last_drift_reminder_at = self._tool_calls_since_update

    def _current_reminder(self) -> dict[str, Any] | None:
        if not self.todo_list.items:
            return None
        if self.todo_list.all_completed and not self.todo_list.verified:
            return {
                "reason": "all_completed_but_unverified",
                "message": (
                    "Every TODO item is marked completed, but no verification is "
                    "recorded. Run relevant checks before claiming completion, then "
                    "call record_todo_verification with concrete evidence."
                ),
                **self.todo_list.snapshot(),
            }

        reminder_due = (
            self._tool_calls_since_update >= self.tool_calls_without_update
            and self._tool_calls_since_update - self._last_drift_reminder_at
            >= self.tool_calls_without_update
        )
        if reminder_due:
            return {
                "reason": "todo_list_stale",
                "message": (
                    "The TODO list has not changed during several tool calls. Review "
                    "the current plan and call update_todo_list if progress or scope "
                    "has changed."
                ),
                "tool_calls_since_update": self._tool_calls_since_update,
                **self.todo_list.snapshot(),
            }
        return None


def todo_tools(todo_list: TodoList) -> list[FunctionTool]:
    """Build the tools that read and mutate a shared TODO list."""

    def update_todo_list(items: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            return todo_list.replace(items)
        except ValueError as exc:
            raise ToolExecutionError(str(exc)) from exc

    def get_todo_list() -> dict[str, Any]:
        return {"ok": True, **todo_list.snapshot()}

    def record_todo_verification(evidence: str) -> dict[str, Any]:
        try:
            return todo_list.record_verification(evidence)
        except ValueError as exc:
            raise ToolExecutionError(str(exc)) from exc

    item_schema = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "A concise, outcome-oriented task description.",
            },
            "status": {
                "type": "string",
                "enum": [status.value for status in TodoStatus],
                "description": "The task's current lifecycle status.",
            },
        },
        "required": ["content", "status"],
        "additionalProperties": False,
    }
    return [
        FunctionTool(
            name=UPDATE_TODO_LIST_TOOL,
            description=(
                "Create or replace the global TODO list. Send the complete current "
                "list whenever task scope or progress changes. Keep exactly one item "
                "in_progress at a time unless parallel work genuinely requires more."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": item_schema,
                        "minItems": 1,
                        "maxItems": MAX_TODO_ITEMS,
                    }
                },
                "required": ["items"],
                "additionalProperties": False,
            },
            handler=update_todo_list,
            strict=True,
        ),
        FunctionTool(
            name=GET_TODO_LIST_TOOL,
            description="Return the global TODO list and its verification state.",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            handler=get_todo_list,
            strict=True,
        ),
        FunctionTool(
            name=RECORD_TODO_VERIFICATION_TOOL,
            description=(
                "Record concrete verification evidence after every TODO item is "
                "completed. Call this only after actually running relevant checks; "
                "any later list change invalidates the recorded verification."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "evidence": {
                        "type": "string",
                        "description": (
                            "Concrete checks and outcomes, such as test command and "
                            "passing result."
                        ),
                    }
                },
                "required": ["evidence"],
                "additionalProperties": False,
            },
            handler=record_todo_verification,
            strict=True,
        ),
    ]
