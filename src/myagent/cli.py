"""Command-line interface for MyAgent."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .agent import AgentLoop, AgentLoopLimitError
from .composition import AgentConfig, create_default_agent
from .mcp import parse_mcp_servers
from .permissions import ApprovalRequest


CONFIG_PATH = Path("myagent.config.json")
_CONFIG_FIELDS = frozenset(
    {
        "api_key",
        "base_url",
        "model",
        "fallback_model",
        "max_tool_rounds",
        "bash_timeout_seconds",
        "todo_reminder_tool_calls",
        "subagent_max_workers",
        "subagent_max_tasks",
        "mcp_servers",
    }
)
_POSITIVE_INTEGER_FIELDS = frozenset(
    {
        "bash_timeout_seconds",
        "todo_reminder_tool_calls",
        "subagent_max_workers",
        "subagent_max_tasks",
    }
)


def build_parser() -> argparse.ArgumentParser:
    file_config = _load_config()
    defaults = AgentConfig()
    configured_model = file_config.get("model", defaults.model)
    fallback_model = file_config.get("fallback_model", defaults.fallback_model)
    parser = argparse.ArgumentParser(description="Run the MyAgent CLI")
    parser.set_defaults(file_config=file_config, fallback_model=fallback_model)
    parser.add_argument("prompt", nargs="*", help="Run one task and exit")
    parser.add_argument(
        "--model",
        default=os.getenv("OPENAI_MODEL", configured_model),
        help="OpenAI model ID (default: %(default)s)",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    # Import lazily so local unit tests for the loop and tools do not require the SDK.
    from openai import OpenAI

    agent = create_default_agent(
        OpenAI(**_client_options(args.file_config)),
        config=_config_from_environment(
            args.model,
            args.fallback_model,
            args.file_config,
        ),
        approval_callback=_ask_user_approval,
    )
    try:
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
    finally:
        agent.close()


def _run_turn(agent: AgentLoop, user_input: str) -> None:
    try:
        answer = agent.run(user_input)
    except AgentLoopLimitError as exc:
        print(f"agent error: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"request failed: {exc}", file=sys.stderr)
    else:
        print(f"agent> {answer}")


def _load_config(
    path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    selected_path = CONFIG_PATH if path is None else Path(path)
    try:
        content = selected_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SystemExit(f"Cannot read config {selected_path}: {exc}") from exc

    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid config {selected_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"Invalid config {selected_path}: expected an object")
    unknown = sorted(set(payload).difference(_CONFIG_FIELDS))
    if unknown:
        raise SystemExit(
            f"Invalid config {selected_path}: unsupported fields "
            f"{', '.join(unknown)}"
        )

    for field in ("model", "fallback_model"):
        if field not in payload:
            continue
        value = payload[field]
        if not isinstance(value, str) or not value.strip():
            raise SystemExit(
                f"Invalid config {selected_path}: {field} must be a non-empty string"
            )
    for field in ("api_key", "base_url"):
        value = payload.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise SystemExit(
                f"Invalid config {selected_path}: {field} must be null or a "
                "non-empty string"
            )
    for field in _POSITIVE_INTEGER_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        if (
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
        ):
            raise SystemExit(
                f"Invalid config {selected_path}: {field} must be a positive integer"
            )
    max_tool_rounds = payload.get("max_tool_rounds")
    if "max_tool_rounds" in payload and (
        not isinstance(max_tool_rounds, int)
        or isinstance(max_tool_rounds, bool)
        or max_tool_rounds < 0
    ):
        raise SystemExit(
            f"Invalid config {selected_path}: max_tool_rounds must be a "
            "non-negative integer"
        )
    if "mcp_servers" in payload:
        try:
            payload["mcp_servers"] = parse_mcp_servers(payload["mcp_servers"])
        except ValueError as exc:
            raise SystemExit(f"Invalid config {selected_path}: {exc}") from exc
    return payload


def _client_options(file_config: dict[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {"max_retries": 0}
    api_key = os.getenv("OPENAI_API_KEY") or file_config.get("api_key")
    base_url = os.getenv("OPENAI_BASE_URL") or file_config.get("base_url")
    if api_key is not None:
        options["api_key"] = api_key
    if base_url is not None:
        options["base_url"] = base_url
    return options


def _config_from_environment(
    model: str,
    fallback_model: str | None = None,
    file_config: dict[str, Any] | None = None,
) -> AgentConfig:
    selected_file_config = {} if file_config is None else file_config
    selected_fallback = (
        AgentConfig().fallback_model if fallback_model is None else fallback_model
    )
    defaults = AgentConfig(model=model, fallback_model=selected_fallback)

    def configured_integer(field: str, environment_name: str) -> int:
        configured = selected_file_config.get(field, getattr(defaults, field))
        return int(os.getenv(environment_name, str(configured)))

    try:
        return AgentConfig(
            model=model,
            fallback_model=selected_fallback,
            max_tool_rounds=configured_integer(
                "max_tool_rounds",
                "AGENT_MAX_TOOL_ROUNDS",
            ),
            bash_timeout_seconds=configured_integer(
                "bash_timeout_seconds",
                "BASH_TIMEOUT_SECONDS",
            ),
            todo_reminder_tool_calls=configured_integer(
                "todo_reminder_tool_calls",
                "TODO_REMINDER_TOOL_CALLS",
            ),
            subagent_max_workers=configured_integer(
                "subagent_max_workers",
                "SUBAGENT_MAX_WORKERS",
            ),
            subagent_max_tasks=configured_integer(
                "subagent_max_tasks",
                "SUBAGENT_MAX_TASKS",
            ),
            mcp_servers=tuple(selected_file_config.get("mcp_servers", ())),
        )
    except ValueError as exc:
        raise SystemExit(
            f"Invalid numeric environment or config value: {exc}"
        ) from exc


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
