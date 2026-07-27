"""Command-line interface for MyAgent."""

from __future__ import annotations

import argparse
import os
import sys

from .agent import AgentLoop, AgentLoopLimitError
from .tools import BashTool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the MyAgent CLI")
    parser.add_argument("prompt", nargs="*", help="Run one task and exit")
    parser.add_argument(
        "--model",
        default=os.getenv("OPENAI_MODEL", "gpt-5.6-sol"),
        help="OpenAI model ID (default: %(default)s)",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    # Import lazily so local unit tests for the loop and tools do not require the SDK.
    from openai import OpenAI

    try:
        max_tool_rounds = int(os.getenv("AGENT_MAX_TOOL_ROUNDS", "10"))
        bash_timeout = int(os.getenv("BASH_TIMEOUT_SECONDS", "30"))
    except ValueError as exc:
        raise SystemExit(f"Invalid numeric environment variable: {exc}") from exc

    agent = AgentLoop(
        OpenAI(),
        model=args.model,
        max_tool_rounds=max_tool_rounds,
        bash_tool=BashTool(timeout_seconds=bash_timeout),
    )

    if args.prompt:
        _run_turn(agent, " ".join(args.prompt))
        return

    print(f"MyAgent ({args.model}). Type 'exit' or 'quit' to leave.")
    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if user_input.lower() in {"exit", "quit"}:
            return
        if not user_input:
            continue
        _run_turn(agent, user_input)


def _run_turn(agent: AgentLoop, user_input: str) -> None:
    try:
        answer = agent.run(user_input)
    except AgentLoopLimitError as exc:
        print(f"agent error: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"request failed: {exc}", file=sys.stderr)
    else:
        print(f"agent> {answer}")
