"""DeepSeek Chat Completions facade for the Responses-based AgentLoop."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4


DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_RELEASE_LABEL = "DeepSeek-V4-Flash-0731"


class DeepSeekResponsesClient:
    """Expose a Responses-like endpoint backed by DeepSeek chat completions."""

    def __init__(self, chat_completions: Any, *, thinking: str = "enabled") -> None:
        self.responses = DeepSeekResponsesEndpoint(
            chat_completions,
            thinking=thinking,
        )


class DeepSeekResponsesEndpoint:
    """Translate MyAgent's Responses request/response subset."""

    def __init__(self, chat_completions: Any, *, thinking: str = "enabled") -> None:
        if thinking not in {"enabled", "disabled"}:
            raise ValueError("thinking must be enabled or disabled")
        self._chat_completions = chat_completions
        self._thinking = thinking
        self._response_messages: dict[str, list[dict[str, Any]]] = {}

    def create(self, **kwargs: Any) -> Any:
        previous_response_id = kwargs.get("previous_response_id")
        if previous_response_id:
            try:
                messages = [
                    dict(message)
                    for message in self._response_messages[str(previous_response_id)]
                ]
            except KeyError as exc:
                raise RuntimeError(
                    "Unknown DeepSeek continuation response id: "
                    f"{previous_response_id}"
                ) from exc
            messages.extend(_messages_from_input(kwargs.get("input", ())))
        else:
            messages = []
            instructions = kwargs.get("instructions")
            if isinstance(instructions, str) and instructions.strip():
                messages.append({"role": "system", "content": instructions})
            messages.extend(_messages_from_input(kwargs.get("input", ())))

        request: dict[str, Any] = {
            "model": kwargs["model"],
            "messages": messages,
            "extra_body": {"thinking": {"type": self._thinking}},
        }
        tools = kwargs.get("tools") or []
        if tools:
            request["tools"] = [_chat_tool(tool) for tool in tools]
        max_output_tokens = kwargs.get("max_output_tokens")
        if isinstance(max_output_tokens, int):
            request["max_tokens"] = max_output_tokens

        response = self._chat_completions.create(**request)
        choices = getattr(response, "choices", None)
        if not choices:
            raise RuntimeError("DeepSeek returned no choices")
        choice = choices[0]
        message = choice.message
        content = getattr(message, "content", None) or ""
        reasoning_content = getattr(message, "reasoning_content", None)
        tool_calls = [_tool_call_payload(call) for call in (message.tool_calls or [])]
        assistant_message = {
            "role": "assistant",
            "content": content,
            "reasoning_content": reasoning_content,
            "tool_calls": tool_calls,
        }

        response_id = getattr(response, "id", None) or f"deepseek-{uuid4().hex}"
        self._response_messages[str(response_id)] = [*messages, assistant_message]
        output: list[Any] = [
            SimpleNamespace(
                type="message",
                role="assistant",
                content=content,
                reasoning_content=reasoning_content,
                tool_calls=tool_calls,
            )
        ]
        for call in tool_calls:
            function = call["function"]
            output.append(
                SimpleNamespace(
                    type="function_call",
                    call_id=call["id"],
                    name=function["name"],
                    arguments=function["arguments"],
                )
            )

        finish_reason = getattr(choice, "finish_reason", None)
        return SimpleNamespace(
            id=response_id,
            model=getattr(response, "model", kwargs["model"]),
            system_fingerprint=getattr(response, "system_fingerprint", None),
            status="incomplete" if finish_reason == "length" else "completed",
            output=output,
            output_text=content,
            usage=_normalized_usage(getattr(response, "usage", None)),
        )


def _messages_from_input(items: Any) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for item in items or ():
        if isinstance(item, dict):
            role = item.get("role")
            if role in {"user", "system"}:
                messages.append({"role": role, "content": item.get("content", "")})
                continue
            if item.get("type") == "function_call_output":
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": item.get("call_id"),
                        "content": item.get("output", ""),
                    }
                )
                continue
        item_type = getattr(item, "type", None)
        if item_type == "message" and getattr(item, "role", None) == "assistant":
            messages.append(
                {
                    "role": "assistant",
                    "content": getattr(item, "content", None) or "",
                    "reasoning_content": getattr(item, "reasoning_content", None),
                    "tool_calls": list(getattr(item, "tool_calls", ()) or ()),
                }
            )
        # Function-call items are represented by the preceding assistant message.
    return messages


def _chat_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters", {"type": "object"}),
        },
    }


def _tool_call_payload(call: Any) -> dict[str, Any]:
    function = call.function
    return {
        "id": call.id,
        "type": "function",
        "function": {
            "name": function.name,
            "arguments": function.arguments,
        },
    }


def _normalized_usage(usage: Any) -> Any:
    if usage is None:
        return None
    return SimpleNamespace(
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
        total_tokens=getattr(usage, "total_tokens", None),
    )
