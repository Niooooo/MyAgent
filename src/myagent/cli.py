"""Command-line interface for MyAgent."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent import AgentLoop, AgentLoopLimitError
from .composition import AgentConfig, create_default_agent
from .mcp import parse_mcp_servers
from .permissions import ApprovalRequest
from .session_store import (
    SessionMetadata,
    SessionStore,
    SessionStoreError,
    StoredSession,
)
from .session_timeline import SessionTimeline


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
    sessions = parser.add_mutually_exclusive_group()
    sessions.add_argument(
        "-c",
        "--continue",
        dest="continue_session",
        action="store_true",
        help="Continue the currently active persistent session",
    )
    sessions.add_argument(
        "-r",
        "--resume",
        metavar="SESSION_ID",
        type=_positive_session_id,
        help="Resume a persistent session by its positive integer ID",
    )
    sessions.add_argument(
        "--no-session",
        action="store_true",
        help="Run without reading or writing persistent session data",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    prompt = " ".join(args.prompt)
    client_options = _client_options(args.file_config)
    sensitive_values = _sensitive_values(client_options)
    cli_session = _prepare_cli_session(
        args,
        prompt=prompt,
        sensitive_values=sensitive_values,
    )

    # Import lazily so local unit tests for the loop and tools do not require the SDK.
    from openai import OpenAI

    agent = create_default_agent(
        OpenAI(**client_options),
        config=_config_from_environment(
            args.model,
            args.fallback_model,
            args.file_config,
        ),
        cwd=cli_session.workspace,
        approval_callback=_ask_user_approval,
        session_timeline=cli_session.timeline,
    )
    try:
        if args.prompt:
            _run_turn(agent, prompt, cli_session)
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
            _run_turn(agent, user_input, cli_session)
    finally:
        agent.close()


def _run_turn(
    agent: AgentLoop,
    user_input: str,
    cli_session: _CLISession | None = None,
) -> None:
    selected = cli_session or _CLISession.temporary(Path.cwd().resolve(), ())
    selected.record_message("你", user_input)
    try:
        answer = agent.run(user_input)
    except AgentLoopLimitError as exc:
        error_text = f"agent error: {selected.redact_error(exc)}"
        print(error_text, file=sys.stderr)
        selected.record_message("错误", error_text)
    except Exception as exc:
        error_text = f"request failed: {selected.redact_error(exc)}"
        print(error_text, file=sys.stderr)
        selected.record_message("错误", error_text)
    else:
        print(f"agent> {answer}")
        selected.record_message("MyAgent", answer)
    selected.persist()


@dataclass
class _CLISession:
    """Own one CLI process's durable boundary without coupling it to AgentLoop."""

    workspace: Path
    timeline: SessionTimeline
    sensitive_values: tuple[str, ...] = field(repr=False)
    store: SessionStore | None = None
    session_id: int | None = None
    revision: int = 0
    messages: list[dict[str, str]] = field(default_factory=list)
    message_cursor: int = 0

    @classmethod
    def temporary(
        cls,
        workspace: Path,
        sensitive_values: tuple[str, ...],
    ) -> _CLISession:
        return cls(workspace, SessionTimeline(), sensitive_values)

    def record_message(self, speaker: str, text: str) -> None:
        self.messages.append({"speaker": speaker, "text": text})

    def redact_error(self, error: BaseException) -> str:
        detail = str(error) or type(error).__name__
        for value in self.sensitive_values:
            if value:
                detail = detail.replace(value, "[redacted]")
        return detail

    def persist(self) -> bool:
        if self.store is None or self.session_id is None:
            return True
        pending = self.timeline.pending_operations
        if not pending:
            return True
        messages = self.messages[self.message_cursor :]
        try:
            result = self.store.append_turn(
                self.session_id,
                messages,
                expected_revision=self.revision,
                pending_operations=pending,
                sensitive_values=self.sensitive_values,
            )
        except Exception as exc:
            print(
                f"session persist failed: {self.redact_error(exc)}",
                file=sys.stderr,
            )
            return False
        self.timeline.acknowledge_operations(result.through_operation_id)
        self.revision = result.new_revision
        self.message_cursor = len(self.messages)
        return True


def _prepare_cli_session(
    args: argparse.Namespace,
    *,
    prompt: str,
    sensitive_values: tuple[str, ...],
) -> _CLISession:
    launch_workspace = Path.cwd().resolve()
    if args.no_session:
        return _CLISession.temporary(launch_workspace, sensitive_values)

    store = SessionStore(sensitive_values=sensitive_values)
    try:
        catalog = store.initialize(sensitive_values=sensitive_values)
        if args.continue_session or args.resume is not None:
            session_id = (
                catalog.active_session_id
                if args.continue_session
                else args.resume
            )
            if session_id is None:
                raise SessionStoreError("no active session to continue")
            stored = store.load_session(session_id)
            workspace = Path(stored.metadata.workspace)
            if not workspace.is_dir():
                raise SessionStoreError(
                    f"session workspace does not exist: {workspace}"
                )
            if args.resume is not None:
                store.activate_session(
                    session_id,
                    sensitive_values=sensitive_values,
                )
            stored = _update_session_model(
                store,
                stored,
                args.model,
                sensitive_values,
            )
        else:
            metadata = SessionMetadata(
                _new_session_title(prompt, launch_workspace),
                str(launch_workspace),
                main_model=args.model,
            )
            stored = store.create_session(
                metadata,
                sensitive_values=sensitive_values,
            )
            workspace = launch_workspace
    except (OSError, SessionStoreError, ValueError) as exc:
        detail = str(exc) or type(exc).__name__
        for value in sensitive_values:
            if value:
                detail = detail.replace(value, "[redacted]")
        raise SystemExit(f"session error: {detail}") from None

    return _CLISession(
        workspace,
        stored.timeline,
        sensitive_values,
        store=store,
        session_id=stored.id,
        revision=stored.revision,
        messages=list(stored.messages),
        message_cursor=len(stored.messages),
    )


def _update_session_model(
    store: SessionStore,
    stored: StoredSession,
    model: str,
    sensitive_values: tuple[str, ...],
) -> StoredSession:
    if stored.metadata.main_model == model:
        return stored
    metadata = SessionMetadata(
        stored.metadata.title,
        stored.metadata.workspace,
        main_model=model,
        sub_model=stored.metadata.sub_model,
        kind=stored.metadata.kind,
    )
    revision = store.append_metadata(
        stored.id,
        metadata,
        expected_revision=stored.revision,
        sensitive_values=sensitive_values,
    )
    return StoredSession(
        stored.id,
        stored.created_at,
        metadata,
        stored.messages,
        stored.timeline,
        revision,
    )


def _new_session_title(prompt: str, workspace: Path) -> str:
    normalized = " ".join(prompt.split())
    if normalized:
        return normalized[:80]
    return f"CLI · {workspace.name or workspace.drive}"[:80]


def _positive_session_id(value: str) -> int:
    try:
        selected = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "SESSION_ID must be a positive integer"
        ) from None
    if selected <= 0:
        raise argparse.ArgumentTypeError("SESSION_ID must be a positive integer")
    return selected


def _sensitive_values(client_options: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            value
            for field in ("api_key", "base_url")
            if isinstance((value := client_options.get(field)), str) and value
        )
    )


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
