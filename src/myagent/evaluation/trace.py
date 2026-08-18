"""In-memory observability for evaluation runs."""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from ..hooks import HookRegistry, PostToolUse, PreToolUse, Stop


class EvaluationRecorder:
    """Collect lightweight hook events without doing I/O inside hook callbacks."""

    def __init__(self) -> None:
        self.pre_tool_events: list[dict[str, Any]] = []
        self.post_tool_events: list[dict[str, Any]] = []
        self.stop_events: list[dict[str, Any]] = []

    def register(self, hooks: HookRegistry) -> None:
        hooks.register(PreToolUse, self._record_pre_tool)
        hooks.register(PostToolUse, self._record_post_tool)
        hooks.register(Stop, self._record_stop)

    def _record_pre_tool(self, event: PreToolUse) -> None:
        self.pre_tool_events.append(
            {
                "tool_name": event.tool_name,
                "arguments": dict(event.arguments),
                "call_id": event.call_id,
                "permission_level": event.permission_level,
                "permission_reason": event.permission_reason,
                "denial_reason": event.denial_reason,
            }
        )

    def _record_post_tool(self, event: PostToolUse) -> None:
        self.post_tool_events.append(
            {
                "tool_name": event.tool_name,
                "arguments": dict(event.arguments),
                "call_id": event.call_id,
                "result": copy.deepcopy(event.result),
            }
        )

    def _record_stop(self, event: Stop) -> None:
        self.stop_events.append(
            {
                "reason": event.reason.value,
                "tool_rounds": event.tool_rounds,
                "output_text": event.output_text,
                "error_type": (
                    type(event.error).__name__ if event.error is not None else None
                ),
                "error": str(event.error) if event.error is not None else None,
            }
        )


class RecordingResponses:
    """Wrap a Responses endpoint and record latency plus optional usage."""

    def __init__(self, endpoint: Any) -> None:
        self._endpoint = endpoint
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        started = time.perf_counter()
        record = {
            "model": kwargs.get("model"),
            "input_items": len(kwargs.get("input", ())),
            "tool_definitions": len(kwargs.get("tools", ())),
            "max_output_tokens": kwargs.get("max_output_tokens"),
            "previous_response_id": kwargs.get("previous_response_id"),
        }
        try:
            response = self._endpoint.create(**kwargs)
        except Exception as exc:
            record.update(
                {
                    "duration_ms": _elapsed_ms(started),
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "usage": None,
                }
            )
            self.requests.append(record)
            raise
        record.update(
            {
                "duration_ms": _elapsed_ms(started),
                "status": "completed",
                "response_id": getattr(response, "id", None),
                "returned_model": getattr(response, "model", None),
                "system_fingerprint": getattr(response, "system_fingerprint", None),
                "usage": extract_usage(getattr(response, "usage", None)),
            }
        )
        self.requests.append(record)
        return response


class RecordingClient:
    """Client facade that exposes a recording Responses endpoint."""

    def __init__(self, client: Any) -> None:
        self.responses = RecordingResponses(client.responses)


class ReplayResponses:
    """Serve a fixed Responses API transcript without network access."""

    def __init__(self, responses: tuple[dict[str, Any], ...]) -> None:
        self._responses = responses
        self._index = 0

    @property
    def consumed(self) -> int:
        return self._index

    @property
    def total(self) -> int:
        return len(self._responses)

    def create(self, **_kwargs: Any) -> SimpleNamespace:
        if self._index >= len(self._responses):
            raise RuntimeError("Replay response script was exhausted")
        specification = self._responses[self._index]
        self._index += 1
        output: list[SimpleNamespace] = []
        for raw_item in specification["output"]:
            item = dict(raw_item)
            if item["type"] == "function_call":
                arguments = item.pop("arguments", None)
                arguments_raw = item.pop("arguments_raw", None)
                item["arguments"] = (
                    arguments_raw
                    if arguments_raw is not None
                    else json.dumps(arguments, ensure_ascii=False)
                )
            output.append(SimpleNamespace(**item))
        return SimpleNamespace(
            output=output,
            output_text=specification.get("output_text", ""),
        )


class ReplayClient:
    """Responses client backed by a :class:`ReplayResponses` transcript."""

    def __init__(self, responses: tuple[dict[str, Any], ...]) -> None:
        self.responses = ReplayResponses(responses)


def extract_usage(usage: Any) -> dict[str, int | None] | None:
    """Normalize the token fields used by OpenAI and compatible providers."""
    if usage is None:
        return None

    def value(name: str) -> int | None:
        selected = usage.get(name) if isinstance(usage, Mapping) else getattr(usage, name, None)
        return selected if isinstance(selected, int) and not isinstance(selected, bool) else None

    normalized = {
        "input_tokens": value("input_tokens"),
        "output_tokens": value("output_tokens"),
        "total_tokens": value("total_tokens"),
    }
    return normalized if any(item is not None for item in normalized.values()) else None


def protocol_trace(history: list[object]) -> dict[str, Any]:
    """Extract function calls and their protocol outputs from Agent history."""
    calls: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    for item in history:
        if getattr(item, "type", None) == "function_call":
            raw_arguments = getattr(item, "arguments", "")
            try:
                arguments: Any = json.loads(raw_arguments)
            except (TypeError, json.JSONDecodeError):
                arguments = raw_arguments
            calls.append(
                {
                    "call_id": getattr(item, "call_id", None),
                    "name": getattr(item, "name", None),
                    "arguments": arguments,
                }
            )
        elif isinstance(item, dict) and item.get("type") == "function_call_output":
            raw_output = item.get("output")
            try:
                parsed_output: Any = json.loads(raw_output)
            except (TypeError, json.JSONDecodeError):
                parsed_output = raw_output
            outputs.append(
                {
                    "call_id": item.get("call_id"),
                    "result": parsed_output,
                }
            )
    call_ids = [item["call_id"] for item in calls]
    output_ids = [item["call_id"] for item in outputs]
    return {
        "function_calls": calls,
        "function_call_outputs": outputs,
        "call_id_sequence_matches": call_ids == output_ids,
        "missing_outputs": sorted(set(call_ids).difference(output_ids)),
        "orphan_outputs": sorted(set(output_ids).difference(call_ids)),
        "duplicate_output_ids": sorted(
            item for item in set(output_ids) if output_ids.count(item) > 1
        ),
    }


def aggregate_usage(
    requests: list[dict[str, Any]],
) -> dict[str, int | bool | None] | None:
    """Sum complete token usage and label any provider data gap as partial."""
    usages = [request.get("usage") for request in requests]
    if not any(usage is not None for usage in usages):
        return None

    aggregate: dict[str, int | bool | None] = {}
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        complete = all(
            usage is not None and usage.get(field) is not None for usage in usages
        )
        aggregate[field] = (
            sum(int(usage[field]) for usage in usages if usage is not None)
            if complete
            else None
        )
    aggregate["partial"] = any(
        usage is None
        or any(
            usage.get(field) is None
            for field in ("input_tokens", "output_tokens", "total_tokens")
        )
        for usage in usages
    )
    return aggregate


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
