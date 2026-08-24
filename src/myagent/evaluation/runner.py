"""Execute isolated repository tasks and produce machine-readable reports."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
import time
from collections.abc import Callable
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..agent import AgentLoop
from ..composition import build_default_components
from ..hooks import HookRegistry
from ..permissions import ApprovalRequest
from .cases import EvalCase
from .grader import GraderResult, build_acceptance_tool, run_hidden_tests
from .judge import write_judge_packets
from .trace import (
    EvaluationRecorder,
    RecordingClient,
    ReplayClient,
    ReplayResponses,
    aggregate_usage,
    protocol_trace,
)


EVALUATION_INSTRUCTIONS = """You are completing a small repository task in an
evaluation workspace. Inspect the repository, make the smallest correct change, and use
run_acceptance_tests before finishing. Only the exposed workspace tools are available.
Do not claim success if a tool or acceptance test reports a failure.
"""
COMPONENT_EVALUATION_INSTRUCTIONS = """You are being evaluated on disciplined
tool use. Follow the user request exactly. Use only tools that are necessary, respect
dependencies between calls, and do not inspect or modify unrelated files. If the answer
is fully present in the prompt, answer without tools. Do not claim a tool succeeded when
its result reports failure.
"""
EVALUATION_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "run_acceptance_tests",
    }
)
_IGNORED_PARTS = frozenset(
    {".git", ".myagent", "__pycache__", ".pytest_cache", ".mypy_cache"}
)
_MUTATING_TOOLS = frozenset({"write_file", "edit_file"})


def run_case(
    case: EvalCase,
    client: Any,
    *,
    provider: str,
    model: str,
    attempt: int = 1,
) -> dict[str, Any]:
    """Run one case in a fresh temporary copy of its fixture repository."""
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix=f"myagent-eval-{case.id}-") as temporary:
        workspace = Path(temporary) / "workspace"
        shutil.copytree(case.fixture_dir, workspace)
        before = _workspace_snapshot(workspace)
        baseline = (
            run_hidden_tests(
                workspace,
                case.grader_dir,
                timeout_seconds=case.grader_timeout_seconds,
            )
            if case.grader_dir is not None
            else None
        )

        approvals: list[dict[str, Any]] = []

        def approve(request: ApprovalRequest) -> bool:
            approved = request.tool_name in case.allowed_approvals
            approvals.append(
                {
                    "tool_name": request.tool_name,
                    "arguments": dict(request.arguments),
                    "reason": request.reason,
                    "approved": approved,
                }
            )
            return approved

        hooks = HookRegistry()
        recorder = EvaluationRecorder()
        recorder.register(hooks)
        allowed_tools = set(EVALUATION_TOOLS)
        if case.grader_dir is None:
            allowed_tools.discard("run_acceptance_tests")
        components = build_default_components(
            client=None,
            cwd=workspace,
            allowed_tools=allowed_tools,
            approval_callback=approve,
            hooks=hooks,
        )
        validation_runs: list[GraderResult] = []
        if case.grader_dir is not None:
            components.tool_registry.register(
                build_acceptance_tool(
                    workspace,
                    case.grader_dir,
                    timeout_seconds=case.grader_timeout_seconds,
                    results=validation_runs,
                )
            )

        recording_client = RecordingClient(client)
        agent: AgentLoop | None = None
        final_text: str | None = None
        terminal_error: BaseException | None = None
        history: list[object] = []
        try:
            agent = AgentLoop(
                recording_client,
                model=model,
                fallback_model=model,
                instructions=(
                    COMPONENT_EVALUATION_INSTRUCTIONS
                    if case.track == "component"
                    else EVALUATION_INSTRUCTIONS
                ),
                max_tool_rounds=case.max_tool_rounds,
                tool_registry=components.tool_registry,
                hooks=components.hooks,
                instructions_provider=components.instructions_provider,
                context_memory=components.context_memory,
                close_callback=components.close,
            )
            try:
                final_text = agent.run(case.prompt)
            except Exception as exc:
                terminal_error = exc
            history = list(agent.history)
        finally:
            if agent is None:
                components.close()
            else:
                agent.close()

        final_grader = (
            run_hidden_tests(
                workspace,
                case.grader_dir,
                timeout_seconds=case.grader_timeout_seconds,
            )
            if case.grader_dir is not None
            else None
        )
        after = _workspace_snapshot(workspace)
        workspace_diff = _workspace_diff(before, after)

        protocol = protocol_trace(history)
        replay_complete = _replay_complete(client)
        actual_changed_files = workspace_diff["changed_files"]
        expected_changed_files = sorted(case.expected_changed_files)
        approval_policy_passed, approval_policy_detail = _approval_policy(
            protocol,
            recorder,
            approvals,
        )
        validation_passed, validation_detail = _validation_policy(
            recorder,
            validation_runs,
            required=case.require_validation_tool,
        )
        assertions = [
            _assertion(
                "agent_completed",
                terminal_error is None and bool(final_text and final_text.strip()),
                _terminal_detail(terminal_error, final_text),
            ),
            _assertion(
                "validation_after_changes",
                validation_passed,
                validation_detail,
            ),
            _assertion(
                "changed_files_match",
                actual_changed_files == expected_changed_files,
                f"expected={expected_changed_files}, actual={actual_changed_files}",
            ),
            _assertion(
                "call_id_sequence",
                protocol["call_id_sequence_matches"]
                and not protocol["missing_outputs"]
                and not protocol["orphan_outputs"]
                and not protocol["duplicate_output_ids"],
                "every function call must have exactly one output with the same call_id",
            ),
            _assertion(
                "approval_policy",
                approval_policy_passed,
                approval_policy_detail,
            ),
            _assertion(
                "stop_event_once",
                len(recorder.stop_events) == 1,
                f"events={len(recorder.stop_events)}",
            ),
        ]
        if baseline is not None:
            assertions.insert(
                0,
                _assertion(
                    "baseline_state",
                    (
                        not baseline.passed
                        if case.baseline_must_fail
                        else baseline.passed
                    ),
                    "fixture must start in the declared baseline state",
                ),
            )
        if final_grader is not None:
            assertions.insert(
                2,
                _assertion(
                    "hidden_tests_passed",
                    final_grader.passed,
                    f"exit_code={final_grader.exit_code}, "
                    f"timed_out={final_grader.timed_out}",
                ),
            )
        if replay_complete is not None:
            assertions.append(
                _assertion(
                    "replay_consumed",
                    replay_complete,
                    "the deterministic transcript must be consumed exactly",
                )
            )
        passed = all(item["passed"] for item in assertions)
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        return {
            "schema_version": 2,
            "case_id": case.id,
            "category": case.category,
            "track": case.track,
            "attempt": attempt,
            "description": case.description,
            "prompt": case.prompt,
            "provider": provider,
            "model": model,
            "status": "passed" if passed else "failed",
            "duration_ms": duration_ms,
            "terminal": {
                "status": "completed" if terminal_error is None else "raised",
                "final_text": final_text,
                "exception_type": (
                    type(terminal_error).__name__ if terminal_error is not None else None
                ),
                "exception": str(terminal_error) if terminal_error is not None else None,
            },
            "baseline_grader": baseline.as_dict() if baseline is not None else None,
            "final_grader": (
                final_grader.as_dict() if final_grader is not None else None
            ),
            "validation_runs": [result.as_dict() for result in validation_runs],
            "workspace_diff": workspace_diff,
            "metrics": {
                "model_requests": len(recording_client.responses.requests),
                "tool_rounds": (
                    recorder.stop_events[0]["tool_rounds"]
                    if len(recorder.stop_events) == 1
                    else None
                ),
                "function_calls": len(protocol["function_calls"]),
                "function_call_outputs": len(protocol["function_call_outputs"]),
                "call_output_coverage": _coverage(protocol),
                "approval_requests": len(approvals),
                "validation_runs": len(validation_runs),
                "token_usage": aggregate_usage(recording_client.responses.requests),
                "cost_usd": None,
            },
            "provider_metadata": {
                "returned_models": sorted(
                    {
                        request["returned_model"]
                        for request in recording_client.responses.requests
                        if request.get("returned_model")
                    }
                ),
                "system_fingerprints": sorted(
                    {
                        request["system_fingerprint"]
                        for request in recording_client.responses.requests
                        if request.get("system_fingerprint")
                    }
                ),
            },
            "expectations": {
                "required_tools": list(case.required_tools),
                "order_constraints": [
                    list(constraint) for constraint in case.order_constraints
                ],
                "forbidden_tools": sorted(case.forbidden_tools),
                "max_tool_calls": case.max_tool_calls,
                "success_criteria": list(case.success_criteria),
                "constraints": list(case.constraints),
            },
            "assertions": assertions,
            "failure_reasons": [
                item["name"] for item in assertions if not item["passed"]
            ],
            "trace": {
                "model_requests": recording_client.responses.requests,
                "pre_tool_use": recorder.pre_tool_events,
                "post_tool_use": recorder.post_tool_events,
                "stop": recorder.stop_events,
                "approvals": approvals,
                **protocol,
            },
        }


def run_suite(
    cases: list[EvalCase],
    *,
    provider: str,
    model: str,
    client: Any | None = None,
    client_factory: Callable[[], Any] | None = None,
    repeat: int = 1,
) -> list[dict[str, Any]]:
    """Run selected cases sequentially with replay or a real Responses client."""
    if provider not in {"replay", "openai", "deepseek"}:
        raise ValueError(f"Unsupported evaluation provider: {provider}")
    if repeat < 1 or repeat > 10:
        raise ValueError("repeat must be from 1 to 10")
    if provider in {"openai", "deepseek"} and client is None and client_factory is None:
        raise ValueError(f"The {provider} provider requires a client")

    results: list[dict[str, Any]] = []
    for case in cases:
        if provider == "replay" and not case.replay_responses:
            raise ValueError(f"Case {case.id} has no replay script")
        for attempt in range(1, repeat + 1):
            if provider == "replay":
                case_client = ReplayClient(case.replay_responses)
            elif client_factory is not None:
                case_client = client_factory()
            else:
                case_client = client
            results.append(
                run_case(
                    case,
                    case_client,
                    provider=provider,
                    model=model,
                    attempt=attempt,
                )
            )
    return results


def write_reports(
    output_dir: str | Path,
    results: list[dict[str, Any]],
    *,
    provider: str,
    model: str,
    dataset_path: str | Path,
    release_label: str | None = None,
    thinking: str | None = None,
) -> dict[str, Any]:
    """Write runs.jsonl, summary.json, and summary.md for CI and humans."""
    selected_output = Path(output_dir)
    selected_output.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    passed = sum(result["status"] == "passed" for result in results)
    total = len(results)
    categories: dict[str, dict[str, int | float]] = {}
    for category in sorted({result["category"] for result in results}):
        category_results = [
            result for result in results if result["category"] == category
        ]
        category_passed = sum(
            result["status"] == "passed" for result in category_results
        )
        categories[category] = {
            "total": len(category_results),
            "passed": category_passed,
            "pass_rate": _rate(category_passed, len(category_results)),
        }
    durations = [float(result["duration_ms"]) for result in results]
    by_case = _by_case(results)
    judge_metadata = write_judge_packets(
        selected_output / "judge-packets.jsonl",
        results,
    )
    summary = {
        "schema_version": 2,
        "run_id": run_id,
        "suite": (
            "resume-eval-v2"
            if any(result["track"] == "component" for result in results)
            else "myagent-repository-tasks-v1"
        ),
        "provider": provider,
        "model": model,
        "dataset": str(Path(dataset_path).resolve()),
        "dataset_sha256": _dataset_sha256(Path(dataset_path)),
        "public_benchmark": False,
        "live_model_executed": provider in {"openai", "deepseek"},
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": _rate(passed, total),
        "failed_cases": [
            result["case_id"] for result in results if result["status"] != "passed"
        ],
        "by_category": categories,
        "by_track": _by_track(results),
        "by_case": by_case,
        "pass_at_k_rate": _rate(
            sum(item["pass_at_k"] for item in by_case.values()),
            len(by_case),
        ),
        "pass_power_k_rate": _rate(
            sum(item["pass_power_k"] for item in by_case.values()),
            len(by_case),
        ),
        "latency_ms": {
            "p50": _percentile(durations, 0.50),
            "p95": _percentile(durations, 0.95),
        },
        "token_usage": _suite_usage(results),
        "cost_usd": None,
        "gate_passed": total > 0 and passed == total,
        "provider_metadata": {
            "release_label": release_label,
            "release_label_version_pinned": False if release_label else None,
            "thinking": thinking,
            "returned_models": sorted(
                {
                    value
                    for result in results
                    for value in result["provider_metadata"]["returned_models"]
                }
            ),
            "system_fingerprints": sorted(
                {
                    value
                    for result in results
                    for value in result["provider_metadata"]["system_fingerprints"]
                }
            ),
        },
        "judge": {
            **judge_metadata,
            "status": "pending_codex_verdicts",
        },
    }
    runs_path = selected_output / "runs.jsonl"
    runs_path.write_text(
        "".join(
            json.dumps({"run_id": run_id, **result}, ensure_ascii=False) + "\n"
            for result in results
        ),
        encoding="utf-8",
    )
    (selected_output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (selected_output / "summary.md").write_text(
        _summary_markdown(summary, results),
        encoding="utf-8",
    )
    return summary


def _workspace_snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        if not path.is_file() or path.suffix in {".pyc", ".pyo"}:
            continue
        snapshot[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def _workspace_diff(before: dict[str, str], after: dict[str, str]) -> dict[str, Any]:
    before_paths = set(before)
    after_paths = set(after)
    created = sorted(after_paths.difference(before_paths))
    deleted = sorted(before_paths.difference(after_paths))
    modified = sorted(
        path for path in before_paths.intersection(after_paths) if before[path] != after[path]
    )
    return {
        "created": created,
        "modified": modified,
        "deleted": deleted,
        "changed_files": sorted([*created, *modified, *deleted]),
    }


def _replay_complete(client: Any) -> bool | None:
    endpoint = getattr(client, "responses", None)
    if not isinstance(endpoint, ReplayResponses):
        return None
    return endpoint.consumed == endpoint.total


def _assertion(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail}


def _terminal_detail(error: BaseException | None, final_text: str | None) -> str:
    if error is not None:
        return f"{type(error).__name__}: {error}"
    return "non-empty final text" if final_text and final_text.strip() else "empty final text"


def _coverage(protocol: dict[str, Any]) -> float:
    calls = len(protocol["function_calls"])
    outputs = len(protocol["function_call_outputs"])
    if calls == 0:
        return 1.0
    return round(min(outputs / calls, 1.0), 4)


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _by_case(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        grouped.setdefault(result["case_id"], []).append(result)
    return {
        case_id: {
            "attempts": len(case_results),
            "passed_attempts": sum(
                result["status"] == "passed" for result in case_results
            ),
            "pass_at_k": any(
                result["status"] == "passed" for result in case_results
            ),
            "pass_power_k": all(
                result["status"] == "passed" for result in case_results
            ),
        }
        for case_id, case_results in sorted(grouped.items())
    }


def _by_track(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        grouped.setdefault(result["track"], []).append(result)
    return {
        track: {
            "total": len(track_results),
            "passed": sum(
                result["status"] == "passed" for result in track_results
            ),
            "pass_rate": _rate(
                sum(result["status"] == "passed" for result in track_results),
                len(track_results),
            ),
        }
        for track, track_results in sorted(grouped.items())
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return round(ordered[index], 3)


def _suite_usage(
    results: list[dict[str, Any]],
) -> dict[str, int | bool | None] | None:
    usages = [
        result["metrics"]["token_usage"]
        for result in results
        if result["metrics"].get("token_usage") is not None
    ]
    if not usages:
        return None
    aggregate: dict[str, int | bool | None] = {}
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        complete = len(usages) == len(results) and all(
            usage.get(field) is not None for usage in usages
        )
        aggregate[field] = (
            sum(int(usage[field]) for usage in usages) if complete else None
        )
    aggregate["partial"] = len(usages) != len(results) or any(
        bool(usage.get("partial")) for usage in usages
    )
    return aggregate


def _dataset_sha256(manifest: Path) -> str:
    """Hash the manifest and every fixture, grader, and replay asset stably."""
    root = manifest.resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        if not path.is_file() or path.suffix in {".pyc", ".pyo"}:
            continue
        encoded_path = relative.as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _approval_policy(
    protocol: dict[str, Any],
    recorder: EvaluationRecorder,
    approvals: list[dict[str, Any]],
) -> tuple[bool, str]:
    mutation_calls = [
        item
        for item in protocol["function_calls"]
        if item["name"] in _MUTATING_TOOLS
    ]
    mutation_events = [
        item
        for item in recorder.pre_tool_events
        if item["tool_name"] in _MUTATING_TOOLS
    ]
    counts_match = len(mutation_calls) == len(mutation_events) == len(approvals)
    entries_match = counts_match and all(
        call["name"] == event["tool_name"] == approval["tool_name"]
        and call["arguments"] == event["arguments"] == approval["arguments"]
        and event["permission_level"] == "allow"
        and approval["approved"]
        for call, event, approval in zip(mutation_calls, mutation_events, approvals)
    )
    return (
        entries_match,
        "mutation_calls="
        f"{len(mutation_calls)}, permission_events={len(mutation_events)}, "
        f"approved_requests={sum(item['approved'] for item in approvals)}",
    )


def _validation_policy(
    recorder: EvaluationRecorder,
    validation_runs: list[GraderResult],
    *,
    required: bool,
) -> tuple[bool, str]:
    if not required:
        return True, "validation tool is optional for this case"
    mutation_indices = [
        index
        for index, item in enumerate(recorder.post_tool_events)
        if item["tool_name"] in _MUTATING_TOOLS
    ]
    validation_indices = [
        index
        for index, item in enumerate(recorder.post_tool_events)
        if item["tool_name"] == "run_acceptance_tests"
    ]
    last_validation_passed = bool(validation_runs and validation_runs[-1].passed)
    validation_is_last = bool(validation_indices) and (
        not mutation_indices or validation_indices[-1] > mutation_indices[-1]
    )
    return (
        last_validation_passed and validation_is_last,
        f"calls={len(validation_runs)}, last_passed={last_validation_passed}, "
        f"after_last_mutation={validation_is_last}",
    )


def _summary_markdown(
    summary: dict[str, Any],
    results: list[dict[str, Any]],
) -> str:
    lines = [
        "# MyAgent evaluation summary",
        "",
        f"- Run: `{summary['run_id']}`",
        f"- Provider: `{summary['provider']}`",
        f"- Model: `{summary['model']}`",
        f"- Result: **{summary['passed']}/{summary['total']} passed**",
        f"- Live model executed: `{str(summary['live_model_executed']).lower()}`",
        "- Token usage: `null` means the provider did not expose usage.",
        "- Cost: `null` because no pricing table is embedded in the evaluator.",
        "",
        "| Case | Category | Status | Duration (ms) | Failures |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for result in results:
        failures = ", ".join(result["failure_reasons"]) or "-"
        lines.append(
            f"| `{result['case_id']}` | {result['category']} | "
            f"{result['status']} | {result['duration_ms']} | {failures} |"
        )
    lines.extend(
        [
            "",
            "> This is the self-built MyAgent pilot set, not a public benchmark score.",
            "",
        ]
    )
    return "\n".join(lines)
