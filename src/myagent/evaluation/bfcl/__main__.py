"""python -m myagent.evaluation.bfcl validate|run."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from ..cli import _selected_model
from .clients import create_client
from .experiment import run_case
from .reporting import write_report
from .schema import DEFAULT_DATASET, DIMENSIONS, dataset_hash, implementation_hash, load_cases, validate_cases


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    for command in ("validate", "run"):
        sub = commands.add_parser(command)
        sub.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
        if command == "validate":
            continue
        sub.add_argument("--provider", choices=("replay", "deepseek", "openai"), default="replay")
        sub.add_argument("--model")
        sub.add_argument("--thinking", choices=("enabled", "disabled"), default="enabled")
        sub.add_argument("--repeat", type=int, default=3)
        sub.add_argument("--dimension", action="append", choices=DIMENSIONS)
        sub.add_argument("--case", action="append", dest="case_ids")
        sub.add_argument("--output", type=Path, required=True)
        sub.add_argument("--request-limit", type=int, default=24)
        sub.add_argument("--output-tokens", type=int, default=2048)
        sub.add_argument("--tool-limit", type=int, default=64)
        sub.add_argument("--request-timeout", type=float, default=60)
        sub.add_argument("--resume", action="store_true")
    return root


def git_revision():
    result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def run(args):
    cases = load_cases(args.dataset)
    if args.command == "validate":
        print(json.dumps(validate_cases(cases), indent=2))
        return 0
    if not 1 <= args.repeat <= 10:
        raise ValueError("--repeat must be in 1..10")
    if args.request_limit < 6 or args.output_tokens < 1 or args.tool_limit < 1 or not 0 < args.request_timeout <= 600:
        raise ValueError("--request-limit >= 6, --output-tokens >= 1, --tool-limit >= 1 required")
    dimensions = args.dimension or list(DIMENSIONS)
    selected = [case for case in cases if case["dimension"] in dimensions]
    if args.case_ids:
        if not set(args.case_ids) <= {case["id"] for case in selected}:
            raise ValueError("Unknown --case or case outside selected dimensions")
        selected = [case for case in selected if case["id"] in args.case_ids]
    model = "offline-oracle" if args.provider == "replay" else _selected_model(args.provider, args.model)
    client_factory = None
    if args.provider != "replay":
        client_factory = lambda: create_client(args.provider, args.thinking, args.request_timeout)
    if client_factory:
        client_factory().close()  # Validate credentials/imports before creating incomplete reports.
    config = {"benchmark": "bfcl-local-v1", "mode": args.command, "provider": args.provider,
              "model": model, "thinking": args.thinking, "repeat": args.repeat,
              "request_limit": args.request_limit, "output_tokens": args.output_tokens,
              "request_timeout_seconds": args.request_timeout,
              "tool_limit": args.tool_limit, "dataset_sha256": dataset_hash(args.dataset),
              "implementation_sha256": implementation_hash(),
              "git_revision": git_revision(), "dimensions": sorted(set(dimensions)),
              "case_ids": [case["id"] for case in selected], "variants": ["single"],
              "expected_runs": len(selected) * args.repeat}
    args.output.mkdir(parents=True, exist_ok=True)
    config_path, rows_path = args.output / "config.json", args.output / "runs.jsonl"
    rows = []
    if config_path.exists() or rows_path.exists():
        if not args.resume or not config_path.exists() or not rows_path.exists():
            raise ValueError("Output already exists; choose a new directory or use --resume")
        if json.loads(config_path.read_text(encoding="utf-8")) != config:
            raise ValueError("Resume configuration does not match the existing run")
        rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    done = {(row["case_id"], row["attempt"], row["variant"]) for row in rows}
    if len(done) != len(rows):
        raise ValueError("Duplicate checkpoint run keys")
    jobs = [(case, attempt, "single") for case in selected for attempt in range(1, args.repeat + 1)]
    valid_keys = {(case["id"], attempt, variant) for case, attempt, variant in jobs}
    if not done <= valid_keys:
        raise ValueError("Checkpoint contains unexpected run keys")
    try:
        with rows_path.open("a", encoding="utf-8") as stream:
            for case, attempt, variant in jobs:
                if (case["id"], attempt, variant) in done:
                    continue
                row = run_case(case, provider=args.provider, model=model,
                               attempt=attempt, client_factory=client_factory, request_limit=args.request_limit,
                               output_tokens=args.output_tokens, tool_limit=args.tool_limit)
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
                rows.append(row)
                print(f"{len(rows)}/{config['expected_runs']} {case['id']} {variant}: {'PASS' if row['passed'] else 'FAIL'}", flush=True)
    finally:
        report = write_report(args.output, rows, config)
    return 0 if report["all_passed"] else 1


def main(argv=None):
    try:
        return run(parser().parse_args(argv))
    except (OSError, ValueError) as exc:
        print(f"benchmark error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
