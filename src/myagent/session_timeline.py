"""Append-only protocol timeline for one agent conversation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Any


@dataclass(frozen=True)
class TimelineOperation:
    """One durable-shaped timeline operation.

    Instances returned by :class:`SessionTimeline` are detached copies.  Mutating
    an item nested inside them therefore cannot mutate the live conversation.
    """

    operation_id: int
    kind: str
    items: tuple[object, ...] = ()
    block_ids: tuple[int, ...] = ()
    turn_id: int | None = None


@dataclass(frozen=True)
class TimelineBlockView:
    """Detached protocol-safe block metadata used by context policy."""

    block_id: int
    kind: str
    items: tuple[object, ...]
    turn_id: int | None
    sent: bool
    protocol_complete: bool


@dataclass(frozen=True)
class TimelineSnapshot:
    """Detached lossless snapshot of timeline operations and active provenance."""

    operations: tuple[TimelineOperation, ...]
    active_blocks: tuple[TimelineBlockView, ...]
    pending_from: int = 0


@dataclass
class _TimelineBlock:
    block_id: int
    kind: str
    items: tuple[object, ...]
    turn_id: int | None
    sent: bool
    protocol_complete: bool


class SessionTimeline:
    """The single source of truth for one AgentLoop's Responses protocol state."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._operations: list[TimelineOperation] = []
        self._active_blocks: list[_TimelineBlock] = []
        self._next_operation_id = 1
        self._next_block_id = 1
        self._current_turn = 0
        self._open_exchange_id: int | None = None
        self._pending_from = 0

    @classmethod
    def from_operations(
        cls,
        operations: Sequence[TimelineOperation],
        *,
        acknowledge: bool = True,
    ) -> SessionTimeline:
        """Build a fresh timeline by strictly replaying durable operations."""
        timeline = cls()
        timeline.replay_operations(operations, acknowledge=acknowledge)
        return timeline

    def replay_operations(
        self,
        operations: Sequence[TimelineOperation],
        *,
        acknowledge: bool = True,
    ) -> None:
        """Transactionally replace an empty timeline with replayed operations."""
        if isinstance(operations, (str, bytes)):
            raise TypeError("operations must be a sequence of TimelineOperation values")
        candidate = SessionTimeline()
        for expected_id, operation in enumerate(operations, 1):
            if not isinstance(operation, TimelineOperation):
                raise TypeError("operations must contain TimelineOperation values")
            if type(operation.operation_id) is not int or operation.operation_id != expected_id:
                raise ValueError("operation ids must be consecutive positive integers")
            if not isinstance(operation.kind, str):
                raise TypeError("operation kind must be a string")
            if not isinstance(operation.items, tuple) or not isinstance(operation.block_ids, tuple):
                raise TypeError("operation items and block ids must be tuples")
            if any(type(block_id) is not int or block_id <= 0 for block_id in operation.block_ids):
                raise ValueError("operation block ids must be positive integers")
            if operation.turn_id is not None and (
                type(operation.turn_id) is not int or operation.turn_id <= 0
            ):
                raise ValueError("operation turn ids must be positive integers")
            if operation.kind == "user_input":
                candidate.record_user_input(operation.items)
            elif operation.kind == "response_output":
                candidate.record_response_output(operation.items)
            elif operation.kind == "tool_outputs":
                candidate.record_tool_outputs(operation.items)
            elif operation.kind == "runtime_items":
                candidate.record_runtime_items(operation.items)
            elif operation.kind == "request_succeeded":
                candidate.record_request_succeeded()
            elif operation.kind == "restore":
                candidate.restore_history(operation.items)
            elif operation.kind == "compaction":
                content = (
                    _item_field(operation.items[0], "content")
                    if len(operation.items) == 1
                    else None
                )
                if not isinstance(content, str):
                    raise ValueError("compaction operation is invalid")
                candidate.compact_blocks(operation.block_ids, content)
            elif operation.kind == "emergency_compaction":
                content = (
                    _item_field(operation.items[0], "content")
                    if len(operation.items) == 1
                    else None
                )
                if not isinstance(content, str):
                    raise ValueError("emergency compaction operation is invalid")
                candidate.emergency_compact(content)
            elif operation.kind == "reset":
                candidate.reset()
            else:
                raise ValueError("operations contain an unknown kind")
            if candidate.operations[-1] != operation:
                raise ValueError("operation provenance does not match replayed state")
        with self._lock:
            if self._operations or self._active_blocks:
                raise ValueError("replay requires an empty timeline")
            self._operations = list(candidate.operations)
            self._active_blocks = [
                _TimelineBlock(
                    block.block_id,
                    block.kind,
                    deepcopy(block.items),
                    block.turn_id,
                    block.sent,
                    block.protocol_complete,
                )
                for block in candidate.active_blocks()
            ]
            self._next_operation_id = candidate._next_operation_id
            self._next_block_id = candidate._next_block_id
            self._current_turn = candidate._current_turn
            self._open_exchange_id = candidate._open_exchange_id
            self._pending_from = len(self._operations) if acknowledge else 0

    def record_user_input(self, items: Sequence[object]) -> None:
        recorded = _copy_items(items)
        if not recorded:
            raise ValueError("user input items must not be empty")
        if any(
            _item_type(item) in {"function_call", "function_call_output"}
            for item in recorded
        ):
            raise ValueError("user input must not contain tool protocol items")
        with self._lock:
            self._reject_if_exchange_open("user input")
            self._current_turn += 1
            operation = self._append_operation(
                "user_input",
                recorded,
                turn_id=self._current_turn,
            )
            self._active_blocks.append(
                self._new_block(
                    "user",
                    operation.items,
                    self._current_turn,
                    sent=False,
                    protocol_complete=True,
                )
            )

    def record_response_output(self, items: Sequence[object]) -> None:
        recorded = _copy_items(items)
        if any(_item_type(item) == "function_call_output" for item in recorded):
            raise ValueError("response output must not contain function_call_output")
        call_ids = [
            _item_field(item, "call_id")
            for item in recorded
            if _item_type(item) == "function_call"
        ]
        if any(not isinstance(call_id, str) or not call_id for call_id in call_ids):
            raise ValueError("function calls require non-empty call_id values")
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("function call ids must be unique within a response")
        with self._lock:
            self._reject_if_exchange_open("response output")
            operation = self._append_operation(
                "response_output",
                recorded,
                turn_id=self._current_turn or None,
            )
            if not recorded:
                self._open_exchange_id = None
                return
            has_calls = any(_item_type(item) == "function_call" for item in recorded)
            block = self._new_block(
                "exchange",
                operation.items,
                self._current_turn or None,
                sent=False,
                protocol_complete=not has_calls and _items_have_atomic_protocol(recorded),
            )
            self._active_blocks.append(block)
            self._open_exchange_id = block.block_id if has_calls else None

    def record_tool_outputs(self, items: Sequence[object]) -> None:
        recorded = _copy_items(items)
        if not recorded:
            raise ValueError("tool outputs must close every open function call")
        with self._lock:
            block = self._find_active_block(self._open_exchange_id)
            if block is None:
                raise ValueError("tool outputs require an open function-call exchange")
            expected = [
                _item_field(item, "call_id")
                for item in block.items
                if _item_type(item) == "function_call"
            ]
            delivered = [
                _item_field(item, "call_id")
                for item in recorded
                if _item_type(item) == "function_call_output"
            ]
            if len(delivered) != len(recorded) or any(
                not isinstance(call_id, str) or not call_id for call_id in delivered
            ):
                raise ValueError("tool output items require non-empty call_id values")
            if len(delivered) != len(set(delivered)) or set(delivered) != set(expected):
                raise ValueError("tool outputs must match every open function call exactly once")
            operation = self._append_operation(
                "tool_outputs",
                recorded,
                turn_id=self._current_turn or None,
            )
            block.items = (*block.items, *operation.items)
            block.protocol_complete = _items_have_atomic_protocol(block.items)
            self._open_exchange_id = None

    def record_runtime_items(self, items: Sequence[object]) -> None:
        recorded = _copy_items(items)
        if not recorded:
            return
        if any(
            _item_type(item) in {"function_call", "function_call_output"}
            for item in recorded
        ):
            raise ValueError("runtime items must not contain tool protocol items")
        with self._lock:
            self._reject_if_exchange_open("runtime items")
            operation = self._append_operation(
                "runtime_items",
                recorded,
                turn_id=self._current_turn or None,
            )
            self._active_blocks.append(
                self._new_block(
                    "runtime",
                    operation.items,
                    self._current_turn or None,
                    sent=False,
                    protocol_complete=_items_have_atomic_protocol(recorded),
                )
            )

    def record_request_succeeded(self) -> None:
        """Mark exactly the active blocks included in the successful request sent."""
        with self._lock:
            self._reject_if_exchange_open("request success")
            included = tuple(block.block_id for block in self._active_blocks)
            self._append_operation("request_succeeded", (), block_ids=included)
            included_set = set(included)
            for block in self._active_blocks:
                if block.block_id in included_set:
                    block.sent = True

    def build_model_input(self) -> list[object]:
        """Return a fresh flattened Responses input projection."""
        with self._lock:
            return deepcopy(
                [item for block in self._active_blocks for item in block.items]
            )

    def snapshot_history(self) -> list[object]:
        return self.build_model_input()

    def snapshot(self) -> TimelineSnapshot:
        with self._lock:
            return TimelineSnapshot(
                tuple(_copy_operation(operation) for operation in self._operations),
                self.active_blocks(),
                self._pending_from,
            )

    def restore(self, snapshot: TimelineSnapshot) -> None:
        """Transactionally restore a lossless structured timeline snapshot."""
        if not isinstance(snapshot, TimelineSnapshot):
            raise TypeError("snapshot must be a TimelineSnapshot")
        operations, blocks, replay = _validated_snapshot_state(snapshot)
        restored_blocks = [
            _TimelineBlock(
                block.block_id,
                block.kind,
                deepcopy(block.items),
                block.turn_id,
                block.sent,
                block.protocol_complete,
            )
            for block in blocks
        ]
        with self._lock:
            self._operations = list(operations)
            self._active_blocks = restored_blocks
            self._next_operation_id = replay._next_operation_id
            self._next_block_id = replay._next_block_id
            self._current_turn = replay._current_turn
            self._open_exchange_id = replay._open_exchange_id
            self._pending_from = snapshot.pending_from

    def restore_history(self, history: Sequence[object]) -> None:
        """Legacy flat restore; provenance is reconstructed conservatively."""
        recorded = _copy_items(history)
        _validate_restored_protocol(recorded)
        with self._lock:
            operation = self._append_operation("restore", recorded)
            self._active_blocks = self._rebuild_blocks(operation.items)
            self._current_turn = max(
                (
                    block.turn_id or 0
                    for block in self._active_blocks
                    if block.kind == "user"
                ),
                default=0,
            )
            self._open_exchange_id = next(
                (
                    block.block_id
                    for block in reversed(self._active_blocks)
                    if block.kind == "exchange" and not block.protocol_complete
                ),
                None,
            )

    def compact_blocks(
        self,
        block_ids: Sequence[int],
        summary_content: str,
    ) -> None:
        """Commit one ordinary compaction after its summary has been validated."""
        selected_ids = tuple(block_ids)
        if not selected_ids or any(
            type(block_id) is not int or block_id <= 0 for block_id in selected_ids
        ):
            raise ValueError("compaction block ids must be positive integers")
        if len(set(selected_ids)) != len(selected_ids):
            raise ValueError("compaction block ids must be non-empty and unique")
        summary = _summary_message(summary_content)
        with self._lock:
            self._reject_if_exchange_open("ordinary compaction")
            selected = [
                block for block in self._active_blocks if block.block_id in selected_ids
            ]
            if len(selected) != len(selected_ids):
                raise ValueError("compaction selected an inactive block")
            if any(not block.protocol_complete for block in selected):
                raise ValueError("ordinary compaction requires protocol-complete blocks")
            operation = self._append_operation(
                "compaction",
                (summary,),
                block_ids=selected_ids,
            )
            removed = set(selected_ids)
            remaining = [
                block for block in self._active_blocks if block.block_id not in removed
            ]
            summary_block = self._new_block(
                "summary",
                operation.items,
                None,
                sent=False,
                protocol_complete=True,
            )
            remaining.insert(_summary_insertion_index(remaining), summary_block)
            self._active_blocks = remaining
            if self._open_exchange_id in removed:
                self._open_exchange_id = None

    def emergency_compact(self, summary_content: str) -> None:
        """Commit a whole-active-projection replacement without deleting its log."""
        summary = _summary_message(summary_content)
        with self._lock:
            removed = tuple(block.block_id for block in self._active_blocks)
            operation = self._append_operation(
                "emergency_compaction",
                (summary,),
                block_ids=removed,
            )
            self._active_blocks = [
                self._new_block(
                    "summary",
                    operation.items,
                    None,
                    sent=False,
                    protocol_complete=True,
                )
            ]
            self._open_exchange_id = None

    def reset(self) -> None:
        """Append a reset boundary and make the active model input empty."""
        with self._lock:
            removed = tuple(block.block_id for block in self._active_blocks)
            self._append_operation("reset", (), block_ids=removed)
            self._active_blocks = []
            self._current_turn = 0
            self._open_exchange_id = None

    def active_blocks(self) -> tuple[TimelineBlockView, ...]:
        with self._lock:
            return tuple(
                TimelineBlockView(
                    block_id=block.block_id,
                    kind=block.kind,
                    items=deepcopy(block.items),
                    turn_id=block.turn_id,
                    sent=block.sent,
                    protocol_complete=block.protocol_complete,
                )
                for block in self._active_blocks
            )

    @property
    def operations(self) -> tuple[TimelineOperation, ...]:
        with self._lock:
            return tuple(_copy_operation(operation) for operation in self._operations)

    @property
    def pending_operations(self) -> tuple[TimelineOperation, ...]:
        with self._lock:
            return tuple(
                _copy_operation(operation)
                for operation in self._operations[self._pending_from :]
            )

    def acknowledge_operations(self, through_operation_id: int) -> None:
        """Mark an operation prefix handled by a future persistence adapter."""
        if not isinstance(through_operation_id, int) or isinstance(
            through_operation_id, bool
        ):
            raise TypeError("through_operation_id must be an integer")
        with self._lock:
            known = [
                index
                for index, operation in enumerate(self._operations)
                if operation.operation_id <= through_operation_id
            ]
            self._pending_from = max(self._pending_from, (max(known) + 1) if known else 0)

    @property
    def block_count(self) -> int:
        with self._lock:
            return len(self._active_blocks)

    @property
    def summary_count(self) -> int:
        with self._lock:
            return sum(block.kind == "summary" for block in self._active_blocks)

    def _append_operation(
        self,
        kind: str,
        items: Sequence[object],
        *,
        block_ids: Sequence[int] = (),
        turn_id: int | None = None,
    ) -> TimelineOperation:
        operation = TimelineOperation(
            self._next_operation_id,
            kind,
            tuple(items),
            tuple(block_ids),
            turn_id,
        )
        self._next_operation_id += 1
        self._operations.append(operation)
        return operation

    def _new_block(
        self,
        kind: str,
        items: Sequence[object],
        turn_id: int | None,
        *,
        sent: bool,
        protocol_complete: bool,
    ) -> _TimelineBlock:
        block = _TimelineBlock(
            self._next_block_id,
            kind,
            tuple(items),
            turn_id,
            sent,
            protocol_complete,
        )
        self._next_block_id += 1
        return block

    def _find_active_block(self, block_id: int | None) -> _TimelineBlock | None:
        if block_id is None:
            return None
        return next(
            (block for block in self._active_blocks if block.block_id == block_id),
            None,
        )

    def _reject_if_exchange_open(self, action: str) -> None:
        if self._open_exchange_id is not None:
            raise ValueError(f"cannot record {action} while function calls are open")

    def _rebuild_blocks(self, items: Sequence[object]) -> list[_TimelineBlock]:
        blocks: list[_TimelineBlock] = []
        exchange: list[object] = []
        pending_context: list[object] = []
        turn_id = 0

        def flush_exchange() -> None:
            if not exchange:
                return
            blocks.append(
                self._new_block(
                    "exchange",
                    tuple(exchange),
                    turn_id or None,
                    sent=True,
                    protocol_complete=_items_have_atomic_protocol(exchange),
                )
            )
            exchange.clear()

        def flush_pending_context() -> None:
            if not pending_context:
                return
            blocks.append(
                self._new_block(
                    "exchange",
                    tuple(pending_context),
                    turn_id or None,
                    sent=True,
                    protocol_complete=True,
                )
            )
            pending_context.clear()

        for item in items:
            if _item_field(item, "role") == "user":
                if any(
                    _item_type(entry) in {"function_call", "function_call_output"}
                    for entry in exchange
                ):
                    flush_exchange()
                else:
                    pending_context.extend(exchange)
                    exchange.clear()
                turn_id += 1
                blocks.append(
                    self._new_block(
                        "user",
                        (*pending_context, item),
                        turn_id,
                        sent=True,
                        protocol_complete=True,
                    )
                )
                pending_context.clear()
                continue
            if _is_summary_message(item):
                flush_exchange()
                flush_pending_context()
                blocks.append(
                    self._new_block(
                        "summary",
                        (item,),
                        None,
                        sent=True,
                        protocol_complete=True,
                    )
                )
                continue
            if _is_runtime_message(item):
                flush_exchange()
                flush_pending_context()
                blocks.append(
                    self._new_block(
                        "runtime",
                        (item,),
                        turn_id or None,
                        sent=True,
                        protocol_complete=True,
                    )
                )
                continue
            if _item_field(item, "role") in {"developer", "system"}:
                if any(
                    _item_type(entry) in {"function_call", "function_call_output"}
                    for entry in exchange
                ):
                    flush_exchange()
                else:
                    pending_context.extend(exchange)
                    exchange.clear()
                pending_context.append(item)
                continue
            if pending_context:
                exchange.extend(pending_context)
                pending_context.clear()
            exchange.append(item)
        flush_exchange()
        flush_pending_context()
        return blocks


def _copy_items(items: Sequence[object]) -> tuple[object, ...]:
    if isinstance(items, (str, bytes)):
        raise TypeError("timeline items must be a sequence of protocol objects")
    return tuple(deepcopy(list(items)))


def _copy_operation(operation: TimelineOperation) -> TimelineOperation:
    return TimelineOperation(
        operation.operation_id,
        operation.kind,
        deepcopy(operation.items),
        operation.block_ids,
        operation.turn_id,
    )


def _validated_snapshot_state(
    snapshot: TimelineSnapshot,
) -> tuple[
    tuple[TimelineOperation, ...],
    tuple[TimelineBlockView, ...],
    SessionTimeline,
]:
    allowed_operations = {
        "user_input",
        "response_output",
        "tool_outputs",
        "runtime_items",
        "request_succeeded",
        "restore",
        "compaction",
        "emergency_compaction",
        "reset",
    }
    allowed_blocks = {"user", "exchange", "runtime", "summary"}
    if not isinstance(snapshot.operations, tuple) or not isinstance(
        snapshot.active_blocks, tuple
    ):
        raise TypeError("snapshot collections must be tuples")
    if type(snapshot.pending_from) is not int or not 0 <= snapshot.pending_from <= len(
        snapshot.operations
    ):
        raise ValueError("snapshot pending operation offset is invalid")
    operations: list[TimelineOperation] = []
    for expected_id, operation in enumerate(snapshot.operations, 1):
        if not isinstance(operation, TimelineOperation):
            raise TypeError("snapshot operations must be TimelineOperation values")
        if type(operation.operation_id) is not int or operation.operation_id != expected_id:
            raise ValueError("snapshot operation ids must be consecutive positive integers")
        if operation.kind not in allowed_operations:
            raise ValueError("snapshot contains an unknown operation kind")
        if not isinstance(operation.items, tuple) or not isinstance(operation.block_ids, tuple):
            raise TypeError("snapshot operation items and block ids must be tuples")
        if any(type(block_id) is not int or block_id <= 0 for block_id in operation.block_ids):
            raise ValueError("snapshot operation block ids must be positive integers")
        if operation.turn_id is not None and (
            type(operation.turn_id) is not int or operation.turn_id <= 0
        ):
            raise ValueError("snapshot operation turn ids must be positive integers")
        operations.append(_copy_operation(operation))

    blocks: list[TimelineBlockView] = []
    seen_block_ids: set[int] = set()
    for block in snapshot.active_blocks:
        if not isinstance(block, TimelineBlockView):
            raise TypeError("snapshot blocks must be TimelineBlockView values")
        if type(block.block_id) is not int or block.block_id <= 0 or block.block_id in seen_block_ids:
            raise ValueError("snapshot block ids must be unique positive integers")
        seen_block_ids.add(block.block_id)
        if block.kind not in allowed_blocks:
            raise ValueError("snapshot contains an unknown block kind")
        if not isinstance(block.items, tuple):
            raise TypeError("snapshot block items must be tuples")
        if block.turn_id is not None and (
            type(block.turn_id) is not int or block.turn_id <= 0
        ):
            raise ValueError("snapshot block turn ids must be positive integers")
        if type(block.sent) is not bool or type(block.protocol_complete) is not bool:
            raise TypeError("snapshot block sent and protocol metadata must be booleans")
        blocks.append(deepcopy(block))

    replay = SessionTimeline()
    for operation in operations:
        if operation.kind == "user_input":
            replay.record_user_input(operation.items)
        elif operation.kind == "response_output":
            replay.record_response_output(operation.items)
        elif operation.kind == "tool_outputs":
            replay.record_tool_outputs(operation.items)
        elif operation.kind == "runtime_items":
            replay.record_runtime_items(operation.items)
        elif operation.kind == "request_succeeded":
            replay.record_request_succeeded()
        elif operation.kind == "restore":
            replay.restore_history(operation.items)
        elif operation.kind == "compaction":
            content = _item_field(operation.items[0], "content") if len(operation.items) == 1 else None
            if not isinstance(content, str):
                raise ValueError("snapshot compaction operation is invalid")
            replay.compact_blocks(operation.block_ids, content)
        elif operation.kind == "emergency_compaction":
            content = _item_field(operation.items[0], "content") if len(operation.items) == 1 else None
            if not isinstance(content, str):
                raise ValueError("snapshot emergency compaction operation is invalid")
            replay.emergency_compact(content)
        else:
            replay.reset()
    replay_operations = replay.operations
    replay_blocks = replay.active_blocks()
    if tuple(operations) != replay_operations:
        raise ValueError("snapshot operations do not match replayed provenance")
    if tuple(blocks) != replay_blocks:
        raise ValueError("snapshot active blocks do not match replayed provenance")
    return tuple(operations), tuple(blocks), replay


def _summary_message(content: str) -> dict[str, str]:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("summary_content must be non-empty")
    return {"type": "message", "role": "assistant", "content": content}


def _summary_insertion_index(blocks: Sequence[_TimelineBlock]) -> int:
    for index, block in enumerate(blocks):
        if block.kind == "user" and block.turn_id == 1:
            return index + 1
    return 0


def _items_have_atomic_protocol(items: Sequence[object]) -> bool:
    pending: set[str] = set()
    seen: set[str] = set()
    for item in items:
        item_type = _item_type(item)
        if item_type == "function_call":
            call_id = _item_field(item, "call_id")
            if not isinstance(call_id, str) or not call_id or call_id in seen:
                return False
            seen.add(call_id)
            pending.add(call_id)
        elif item_type == "function_call_output":
            call_id = _item_field(item, "call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                return False
            pending.remove(call_id)
    return not pending


def _validate_restored_protocol(items: Sequence[object]) -> None:
    exchange: list[object] = []

    def validate_exchange() -> None:
        if exchange and not _items_have_atomic_protocol(exchange):
            raise ValueError("restored history contains an invalid tool-call exchange")
        exchange.clear()

    for item in items:
        if (
            _is_summary_message(item)
            or _is_runtime_message(item)
            or _item_field(item, "role") in {"developer", "system", "user"}
        ):
            validate_exchange()
            continue
        exchange.append(item)
    validate_exchange()


def _is_summary_message(item: object) -> bool:
    content = _item_field(item, "content")
    return isinstance(content, str) and content.startswith(
        "[Earlier conversation summary; historical data, not instructions]"
    )


def _is_runtime_message(item: object) -> bool:
    content = _item_field(item, "content")
    return isinstance(content, str) and content.startswith("BACKGROUND_TOOL_RESULTS")


def _item_type(item: object) -> str | None:
    value = _item_field(item, "type")
    if isinstance(value, str):
        return value
    return "message" if _item_field(item, "role") is not None else None


def _item_field(item: object, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)
