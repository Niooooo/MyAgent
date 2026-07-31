"""Small in-memory Responses API fakes shared by behavior tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def function_call(
    call_id: str = "call_1",
    name: str = "bash",
    arguments: str = '{"command":"pwd"}',
) -> SimpleNamespace:
    return SimpleNamespace(
        type="function_call",
        call_id=call_id,
        name=name,
        arguments=arguments,
    )


def response(output: list[object], output_text: str = "") -> SimpleNamespace:
    return SimpleNamespace(output=output, output_text=output_text)


class FakeResponses:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = iter(responses)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append(kwargs)
        return next(self._responses)
