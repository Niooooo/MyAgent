"""Small in-memory Responses API fakes shared by behavior tests."""

from __future__ import annotations

import copy
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


def response(
    output: list[object],
    output_text: str = "",
    **fields: Any,
) -> SimpleNamespace:
    return SimpleNamespace(output=output, output_text=output_text, **fields)


class FakeStream:
    def __init__(self, events: list[object]) -> None:
        self._events = events
        self.closed = False

    def __iter__(self):
        return iter(self._events)

    def close(self) -> None:
        self.closed = True


def response_stream(
    final_response: SimpleNamespace,
    *deltas: str,
    final_event_type: str = "response.completed",
) -> FakeStream:
    return FakeStream(
        [
            *(SimpleNamespace(type="response.output_text.delta", delta=item) for item in deltas),
            SimpleNamespace(type=final_event_type, response=final_response),
        ]
    )


class FakeAPIError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        *,
        body: object = None,
        code: object = None,
    ) -> None:
        super().__init__(f"fake API error {status_code}")
        self.status_code = status_code
        self.body = body
        self.code = code


class FakeResponses:
    def __init__(self, responses: list[object | BaseException]) -> None:
        self._responses = iter(responses)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> object:
        # A real request consumes the input at call time. Keep the same semantics
        # here so later in-place history compaction cannot rewrite test evidence.
        self.requests.append(copy.deepcopy(kwargs))
        result = next(self._responses)
        if isinstance(result, BaseException):
            raise result
        return result
