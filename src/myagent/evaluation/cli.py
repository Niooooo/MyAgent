"""Command-line entry point for MyAgent evaluations."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from ..agent import DEFAULT_MODEL
from .cases import DatasetError, EvalCase, load_cases
from .deepseek import (
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_RELEASE_LABEL,
    DeepSeekResponsesClient,
)
from .judge import aggregate_verdict_files
from .runner import run_suite, write_reports


def default_dataset_path() -> Path:
    """Prefer the current checkout, then the source tree containing this module."""
    relative = Path("evaluations") / "live" / "cases.jsonl"
    current = Path.cwd() / relative
    if current.is_file():
        return current
    return Path(__file__).resolve().parents[3] / relative


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="myagent-eval",
        description="Validate or run MyAgent's isolated repository-task evaluation set",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Validate data and assets only")
    validate.add_argument(
        "--dataset",
        type=Path,
        default=default_dataset_path(),
        help="JSONL case manifest (default: %(default)s)",
    )

    run = subparsers.add_parser("run", help="Run replay smoke or a real model")
    run.add_argument(
        "--dataset",
        type=Path,
        default=default_dataset_path(),
        help="JSONL case manifest (default: %(default)s)",
    )
    run.add_argument(
        "--provider",
        choices=("replay", "openai", "deepseek"),
        default="replay",
        help="replay is offline; openai and deepseek call real models",
    )
    run.add_argument(
        "--model",
        default=None,
        help="Model id (provider-specific default when omitted)",
    )
    run.add_argument(
        "--case",
        action="append",
        dest="case_ids",
        help="Run only this case id; may be repeated",
    )
    run.add_argument(
        "--output",
        type=Path,
        help="Report directory (default: eval-results/<provider>)",
    )
    run.add_argument(
        "--allow-code-execution",
        action="store_true",
        help=(
            "Acknowledge that graders execute code from a temporary workspace; "
            "temporary directories are not an OS sandbox"
        ),
    )
    run.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Attempts per case, from 1 to 10 (default: 1)",
    )
    run.add_argument(
        "--thinking",
        choices=("enabled", "disabled"),
        default="enabled",
        help="DeepSeek thinking mode (default: enabled)",
    )
    run.add_argument(
        "--release-label",
        default=DEFAULT_RELEASE_LABEL,
        help="Human release label recorded separately from the API model id",
    )

    judge = subparsers.add_parser(
        "judge",
        help="Validate Codex verdicts and aggregate the final resume report",
    )
    judge.add_argument("--packets", type=Path, required=True)
    judge.add_argument("--verdicts", type=Path, required=True)
    judge.add_argument("--output", type=Path, required=True)
    judge.add_argument("--run-summary", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "judge":
            summary = aggregate_verdict_files(
                args.packets,
                args.verdicts,
                args.output,
                run_summary_path=args.run_summary,
            )
            print(
                json.dumps(
                    {
                        "status": "passed" if summary["failed"] == 0 else "failed",
                        "passed": summary["passed"],
                        "total": summary["total"],
                        "summary": str(
                            (args.output / "judge-summary.json").resolve()
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if summary["failed"] == 0 else 1

        cases = load_cases(args.dataset)
        if args.command == "validate":
            print(
                json.dumps(
                    {
                        "status": "valid",
                        "schema_versions": sorted(
                            {case.schema_version for case in cases}
                        ),
                        "dataset": str(args.dataset.resolve()),
                        "cases": len(cases),
                        "case_ids": [case.id for case in cases],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if not args.allow_code_execution:
            raise DatasetError(
                "Evaluation graders execute workspace code. Re-run with "
                "--allow-code-execution after reviewing evaluations/README.md."
            )
        selected = _select_cases(cases, args.case_ids)
        model = _selected_model(args.provider, args.model)
        client_factory = None
        if args.provider == "openai":
            client_factory = _openai_client
        elif args.provider == "deepseek":
            client_factory = lambda: _deepseek_client(args.thinking)
        results = run_suite(
            selected,
            provider=args.provider,
            model=model,
            client_factory=client_factory,
            repeat=args.repeat,
        )
        output = args.output or Path("eval-results") / args.provider
        summary = write_reports(
            output,
            results,
            provider=args.provider,
            model=model,
            dataset_path=args.dataset,
            release_label=(
                args.release_label if args.provider == "deepseek" else None
            ),
            thinking=args.thinking if args.provider == "deepseek" else None,
        )
        print(
            json.dumps(
                {
                    "status": "passed" if summary["gate_passed"] else "failed",
                    "passed": summary["passed"],
                    "total": summary["total"],
                    "live_model_executed": summary["live_model_executed"],
                    "summary": str((output / "summary.json").resolve()),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if summary["gate_passed"] else 1
    except (DatasetError, OSError, ValueError) as exc:
        print(f"evaluation error: {exc}", file=sys.stderr)
        return 2


def _select_cases(cases: list[EvalCase], requested: list[str] | None) -> list[EvalCase]:
    if not requested:
        return cases
    duplicates = sorted(
        case_id for case_id in set(requested) if requested.count(case_id) > 1
    )
    if duplicates:
        raise DatasetError(f"Duplicate --case values: {', '.join(duplicates)}")
    by_id = {case.id: case for case in cases}
    unknown = sorted(set(requested).difference(by_id))
    if unknown:
        raise DatasetError(f"Unknown evaluation case ids: {', '.join(unknown)}")
    requested_set = set(requested)
    return [case for case in cases if case.id in requested_set]


def _openai_client() -> Any:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise DatasetError("OPENAI_API_KEY is required for --provider openai")
    from openai import OpenAI

    options: dict[str, Any] = {"api_key": api_key, "max_retries": 0}
    base_url = os.getenv("OPENAI_BASE_URL")
    if base_url:
        options["base_url"] = base_url
    return OpenAI(**options)


def _deepseek_client(thinking: str) -> DeepSeekResponsesClient:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise DatasetError("DEEPSEEK_API_KEY is required for --provider deepseek")
    from openai import OpenAI

    client = OpenAI(
        api_key=api_key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL),
        max_retries=0,
    )
    return DeepSeekResponsesClient(
        client.chat.completions,
        thinking=thinking,
    )


def _selected_model(provider: str, requested: str | None) -> str:
    if requested:
        return requested
    if provider == "deepseek":
        return os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL)
    return os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
