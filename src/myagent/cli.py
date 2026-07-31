"""Command-line interface for MyAgent."""

from __future__ import annotations

import argparse
import json
import os
import sys

from .agent import AgentLoop, AgentLoopLimitError
from .composition import AgentConfig, create_default_agent
from .permissions import ApprovalRequest


def build_parser() -> argparse.ArgumentParser:
    defaults = AgentConfig()
    parser = argparse.ArgumentParser(description="Run the MyAgent CLI")
    parser.add_argument("prompt", nargs="*", help="Run one task and exit")
    parser.add_argument(
        "--model",
        default=os.getenv("OPENAI_MODEL", defaults.model),
        help="OpenAI model ID (default: %(default)s)",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    # Import lazily so local unit tests for the loop and tools do not require the SDK.
    from openai import OpenAI

    agent = create_default_agent(
        OpenAI(),
        config=_config_from_environment(args.model),
        approval_callback=_ask_user_approval,
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


def _config_from_environment(model: str) -> AgentConfig:
    defaults = AgentConfig(model=model)
    try:
        return AgentConfig(
            model=model,
            max_tool_rounds=int(
                os.getenv("AGENT_MAX_TOOL_ROUNDS", str(defaults.max_tool_rounds))
            ),
            bash_timeout_seconds=int(
                os.getenv("BASH_TIMEOUT_SECONDS", str(defaults.bash_timeout_seconds))
            ),
            todo_reminder_tool_calls=int(
                os.getenv(
                    "TODO_REMINDER_TOOL_CALLS",
                    str(defaults.todo_reminder_tool_calls),
                )
            ),
        )
    except ValueError as exc:
        raise SystemExit(f"Invalid numeric environment variable: {exc}") from exc


def _ask_user_approval(request: ApprovalRequest) -> bool:
    """Show a sensitive tool call and request one-time human approval."""
    print("\nApproval required before executing a sensitive operation:")
    print(f"tool: {request.tool_name}")
    print(f"reason: {request.reason}")
    print("arguments:")
    print(json.dumps(dict(request.arguments), ensure_ascii=False, indent=2))
    try:
        answer = input("Approve this operation once? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in {"y", "yes"}
