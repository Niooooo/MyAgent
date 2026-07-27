"""Core Responses API agent loop."""

from __future__ import annotations

import json
from collections.abc import Callable
from os import PathLike
from typing import Any

from .tooling import ToolRegistry
from .tools import build_default_tool_registry


DEFAULT_INSTRUCTIONS = """You are a capable local command-line agent.
Prefer read_file, write_file, edit_file, glob, and grep for workspace file tasks.
Use bash when a shell command is genuinely needed.
Explain the final result clearly and concisely.
If a tool reports an error or refuses a command, do not claim that the command succeeded.
"""


class AgentLoopLimitError(RuntimeError):
    """Raised when the model exceeds the configured number of tool rounds."""


class AgentLoop:
    """Run a stateful OpenAI Responses API tool-calling loop."""

    def __init__(
        self,
        client: Any,
        *,
        model: str = "gpt-5.6-sol",
        instructions: str = DEFAULT_INSTRUCTIONS,
        max_tool_rounds: int = 10,
        bash_tool: Callable[[str], dict[str, Any]] | None = None,
        tool_registry: ToolRegistry | None = None,
        workspace_root: str | PathLike[str] | None = None,
    ) -> None:
        if max_tool_rounds < 0:
            raise ValueError("max_tool_rounds must be non-negative")
        if tool_registry is not None and bash_tool is not None:
            raise ValueError("Pass either tool_registry or bash_tool, not both")

        self.client = client
        self.model = model
        self.instructions = instructions
        self.max_tool_rounds = max_tool_rounds
        self.history: list[Any] = []
        self.tool_registry = tool_registry or build_default_tool_registry(
            cwd=workspace_root,
            bash_tool=bash_tool,
        )

    def run(self, user_input: str) -> str:
        """Process one user turn and return the model's final text."""
        if not user_input.strip():
            raise ValueError("user_input must not be empty")

        self.history.append({"role": "user", "content": user_input})
        tool_rounds = 0

        while True:
            response = self.client.responses.create(
                model=self.model,
                instructions=self.instructions,
                tools=self.tool_registry.definitions,
                input=self.history,
            )

            # Preserve every output item, including reasoning items required by
            # reasoning models on subsequent tool-calling turns.
            self.history.extend(response.output)
            calls = [item for item in response.output if item.type == "function_call"]

            if not calls:
                output_text = response.output_text.strip()
                if not output_text:
                    raise RuntimeError("The model returned neither tool calls nor text")
                return output_text

            if tool_rounds >= self.max_tool_rounds:
                raise AgentLoopLimitError(
                    f"Agent exceeded {self.max_tool_rounds} tool rounds"
                )

            for call in calls:
                result = self._execute_call(call)
                self.history.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": json.dumps(result, ensure_ascii=False),
                    }
                )

            tool_rounds += 1

    def reset(self) -> None:
        """Clear the in-memory conversation history."""
        self.history.clear()

    def _execute_call(self, call: Any) -> dict[str, Any]:
        return self.tool_registry.execute(call.name, call.arguments)
