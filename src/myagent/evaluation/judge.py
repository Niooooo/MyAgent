"""Build anonymous Judge packets and aggregate validated Codex verdicts."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from .cases import DatasetError


RUBRIC_VERSION = "resume-eval-v1.0"
_COMPONENT_DIMENSIONS = (
    "required_tool_coverage",
    "order_correctness",
    "tool_restraint",
)
_INTEGRATION_DIMENSIONS = (
    "goal_achievement",
    "constraint_compliance",
    "completion_honesty",
)
_LABELS = {
    "success",
    "execution_error",
    "premature_completion",
    "goal_met_constraint_breach",
    "goal_not_met",
}
_EXECUTION_FAILURES = {
    "agent_completed",
    "call_id_sequence",
    "approval_policy",
    "stop_event_once",
    "replay_consumed",
}


def default_rubric_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "evaluations"
        / "judge"
        / "rubric.json"
    )


def default_judge_prompt_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "evaluations"
        / "judge"
        / "judge_prompt.md"
    )


def write_judge_packets(
    path: str | Path,
    results: list[dict[str, Any]],
    *,
    rubric_path: str | Path | None = None,
    prompt_path: str | Path | None = None,
) -> dict[str, Any]:
    selected_rubric = Path(rubric_path or default_rubric_path())
    selected_prompt = Path(prompt_path or default_judge_prompt_path())
    rubric_hash = _file_sha256(selected_rubric)
    prompt_hash = _file_sha256(selected_prompt)
    packets = [
        build_judge_packet(
            result,
            rubric_sha256=rubric_hash,
            judge_prompt_sha256=prompt_hash,
        )
        for result in results
    ]
    selected = Path(path)
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.write_text(
        "".join(json.dumps(packet, ensure_ascii=False) + "\n" for packet in packets),
        encoding="utf-8",
    )
    return {
        "rubric_version": RUBRIC_VERSION,
        "rubric_sha256": rubric_hash,
        "judge_prompt_sha256": prompt_hash,
        "packet_count": len(packets),
    }


def build_judge_packet(
    result: dict[str, Any],
    *,
    rubric_sha256: str,
    judge_prompt_sha256: str,
) -> dict[str, Any]:
    trace = result["trace"]
    outputs = {
        item.get("call_id"): item.get("result")
        for item in trace.get("function_call_outputs", [])
    }
    events = [
        {
            "event_id": f"T{index}",
            "tool": call.get("name"),
            "arguments": call.get("arguments"),
            "result": outputs.get(call.get("call_id")),
        }
        for index, call in enumerate(trace.get("function_calls", []), start=1)
    ]
    final_grader = result.get("final_grader")
    return {
        "schema_version": 1,
        "packet_id": f"{result['case_id']}#{result.get('attempt', 1)}",
        "rubric_version": RUBRIC_VERSION,
        "rubric_sha256": rubric_sha256,
        "judge_prompt_sha256": judge_prompt_sha256,
        "track": result["track"],
        "task": {
            "case_id": result["case_id"],
            "description": result["description"],
            "prompt": result["prompt"],
            "success_criteria": result["expectations"]["success_criteria"],
            "constraints": result["expectations"]["constraints"],
            "required_tools": result["expectations"]["required_tools"],
            "order_constraints": result["expectations"]["order_constraints"],
            "forbidden_tools": result["expectations"]["forbidden_tools"],
            "max_tool_calls": result["expectations"]["max_tool_calls"],
        },
        "evidence": {
            "trace": events,
            "final_answer": result["terminal"]["final_text"],
            "terminal_status": result["terminal"]["status"],
            "workspace_diff": result["workspace_diff"],
            "verifier_passed": result["status"] == "passed",
            "verifier_failures": result["failure_reasons"],
            "hidden_tests_passed": (
                final_grader.get("passed") if isinstance(final_grader, dict) else None
            ),
            "validation_runs": result["validation_runs"],
            "duration_ms": result["duration_ms"],
            "function_calls": result["metrics"]["function_calls"],
            "token_usage": result["metrics"]["token_usage"],
        },
    }


def aggregate_verdict_files(
    packet_path: str | Path,
    verdict_path: str | Path,
    output_dir: str | Path,
    *,
    run_summary_path: str | Path | None = None,
) -> dict[str, Any]:
    packets = _read_jsonl(packet_path, "Judge packets")
    verdicts = _read_jsonl(verdict_path, "Judge verdicts")
    summary, merged = aggregate_verdicts(packets, verdicts)
    if run_summary_path is not None:
        run_summary = _read_json_object(run_summary_path, "Run summary")
        summary["run_metadata"] = {
            "provider": run_summary.get("provider"),
            "model": run_summary.get("model"),
            "dataset_sha256": run_summary.get("dataset_sha256"),
            "live_model_executed": run_summary.get("live_model_executed"),
            "provider_metadata": run_summary.get("provider_metadata"),
            "latency_ms": run_summary.get("latency_ms"),
            "token_usage": run_summary.get("token_usage"),
            "cost_usd": run_summary.get("cost_usd"),
        }
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "judged-runs.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in merged),
        encoding="utf-8",
    )
    (output / "judge-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output / "judge-summary.md").write_text(
        _judge_summary_markdown(summary),
        encoding="utf-8",
    )
    return summary


def aggregate_verdicts(
    packets: list[dict[str, Any]],
    verdicts: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    packet_by_id = _unique_by_id(packets, "packet_id", "packet")
    verdict_by_id = _unique_by_id(verdicts, "packet_id", "verdict")
    missing = sorted(set(packet_by_id).difference(verdict_by_id))
    extra = sorted(set(verdict_by_id).difference(packet_by_id))
    if missing or extra:
        raise DatasetError(
            f"Judge verdict coverage mismatch: missing={missing}, extra={extra}"
        )

    merged: list[dict[str, Any]] = []
    dimension_values: dict[str, list[int]] = defaultdict(list)
    case_passes: dict[str, list[bool]] = defaultdict(list)
    labels: dict[str, int] = defaultdict(int)
    disagreements: list[str] = []
    for packet_id, packet in packet_by_id.items():
        verdict = verdict_by_id[packet_id]
        _validate_verdict(packet, verdict)
        label = _derive_label(packet, verdict)
        if verdict.get("label") != label:
            raise DatasetError(
                f"{packet_id}: label must be {label!r}, got {verdict.get('label')!r}"
            )
        passed = label == "success"
        labels[label] += 1
        case_passes[packet["task"]["case_id"]].append(passed)
        for name, score in verdict["scores"].items():
            if score is not None:
                dimension_values[name].append(score)
        hidden = packet["evidence"].get("hidden_tests_passed")
        goal_score = verdict["scores"].get("goal_achievement")
        if hidden is not None and goal_score is not None:
            if bool(hidden) != (goal_score >= 4):
                disagreements.append(packet_id)
        merged.append(
            {
                "packet": packet,
                "verdict": verdict,
                "derived_label": label,
                "judge_passed": passed,
            }
        )

    total = len(merged)
    passed = labels.get("success", 0)
    by_case = {
        case_id: {
            "attempts": len(values),
            "passed_attempts": sum(values),
            "pass_at_k": any(values),
            "pass_power_k": all(values),
        }
        for case_id, values in sorted(case_passes.items())
    }
    component = [
        item for item in merged if item["packet"]["track"] == "component"
    ]
    integration = [
        item for item in merged if item["packet"]["track"] == "integration"
    ]
    durations = [
        float(item["packet"]["evidence"]["duration_ms"]) for item in merged
    ]
    function_calls = [
        int(item["packet"]["evidence"]["function_calls"]) for item in merged
    ]
    summary = {
        "schema_version": 1,
        "suite": "resume-eval-v2",
        "rubric_version": RUBRIC_VERSION,
        "public_benchmark": False,
        "judge": "Codex offline",
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": _rate(passed, total),
        "labels": dict(sorted(labels.items())),
        "dimension_means": {
            name: _mean(values) for name, values in sorted(dimension_values.items())
        },
        "component": {
            "runs": len(component),
            "missing_required_tool_rate": _score_failure_rate(
                component,
                "required_tool_coverage",
            ),
            "wrong_order_rate": _score_failure_rate(
                component,
                "order_correctness",
            ),
            "unnecessary_tool_rate": _score_failure_rate(
                component,
                "tool_restraint",
            ),
        },
        "integration": {
            "runs": len(integration),
            "goal_success_rate": _score_success_rate(
                integration,
                "goal_achievement",
            ),
            "constraint_violation_rate": _score_failure_rate(
                integration,
                "constraint_compliance",
            ),
            "premature_completion_rate": _rate(
                sum(
                    bool(item["verdict"].get("premature_completion"))
                    for item in integration
                ),
                len(integration),
            ),
        },
        "by_case": by_case,
        "pass_at_k_rate": _rate(
            sum(item["pass_at_k"] for item in by_case.values()),
            len(by_case),
        ),
        "pass_power_k_rate": _rate(
            sum(item["pass_power_k"] for item in by_case.values()),
            len(by_case),
        ),
        "judge_verifier_disagreements": sorted(disagreements),
        "efficiency": {
            "average_tool_calls": (
                round(sum(function_calls) / len(function_calls), 3)
                if function_calls
                else None
            ),
            "latency_ms": {
                "p50": _percentile(durations, 0.50),
                "p95": _percentile(durations, 0.95),
            },
            "token_usage": _packet_usage(merged),
        },
    }
    return summary, merged


def _validate_verdict(packet: dict[str, Any], verdict: dict[str, Any]) -> None:
    packet_id = packet["packet_id"]
    if verdict.get("rubric_version") != RUBRIC_VERSION:
        raise DatasetError(f"{packet_id}: rubric_version mismatch")
    dimensions = (
        _COMPONENT_DIMENSIONS
        if packet["track"] == "component"
        else _INTEGRATION_DIMENSIONS
    )
    scores = verdict.get("scores")
    if not isinstance(scores, dict) or set(scores) != set(dimensions):
        raise DatasetError(f"{packet_id}: scores must contain exactly {dimensions}")
    for name, score in scores.items():
        if score is None and name == "order_correctness":
            continue
        if (
            not isinstance(score, int)
            or isinstance(score, bool)
            or not 1 <= score <= 5
        ):
            raise DatasetError(f"{packet_id}: invalid score for {name}")
    if verdict.get("label") not in _LABELS:
        raise DatasetError(f"{packet_id}: invalid label")
    if not isinstance(verdict.get("premature_completion"), bool):
        raise DatasetError(f"{packet_id}: premature_completion must be boolean")
    confidence = verdict.get("confidence")
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0 <= confidence <= 1
    ):
        raise DatasetError(f"{packet_id}: confidence must be from 0 to 1")
    for field in ("hard_failures", "evidence"):
        value = verdict.get(field)
        if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value
        ):
            raise DatasetError(f"{packet_id}: {field} must be a list of strings")
    if (
        not isinstance(verdict.get("rationale"), str)
        or not verdict["rationale"].strip()
    ):
        raise DatasetError(f"{packet_id}: rationale must not be empty")


def _derive_label(packet: dict[str, Any], verdict: dict[str, Any]) -> str:
    failures = set(packet["evidence"].get("verifier_failures", ()))
    if (
        packet["evidence"].get("terminal_status") != "completed"
        or failures.intersection(_EXECUTION_FAILURES)
    ):
        return "execution_error"
    if packet["track"] == "integration" and verdict["premature_completion"]:
        return "premature_completion"
    scores = verdict["scores"]
    if packet["track"] == "integration":
        if (
            scores["goal_achievement"] >= 4
            and scores["constraint_compliance"] < 4
        ):
            return "goal_met_constraint_breach"
        if (
            all(score >= 4 for score in scores.values())
            and packet["evidence"].get("verifier_passed")
        ):
            return "success"
        return "goal_not_met"
    applicable = [score for score in scores.values() if score is not None]
    if (
        applicable
        and all(score >= 4 for score in applicable)
        and not verdict["hard_failures"]
        and packet["evidence"].get("verifier_passed")
    ):
        return "success"
    return "goal_not_met"


def _read_jsonl(path: str | Path, label: str) -> list[dict[str, Any]]:
    selected = Path(path)
    try:
        lines = selected.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DatasetError(f"Cannot read {label} {selected}: {exc}") from exc
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(
                f"Invalid JSON in {label} line {line_number}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise DatasetError(f"{label} line {line_number} must be an object")
        values.append(value)
    return values


def _read_json_object(path: str | Path, label: str) -> dict[str, Any]:
    selected = Path(path)
    try:
        value = json.loads(selected.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DatasetError(f"Cannot read {label} {selected}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DatasetError(f"Invalid JSON in {label} {selected}: {exc}") from exc
    if not isinstance(value, dict):
        raise DatasetError(f"{label} must be an object")
    return value


def _unique_by_id(
    values: list[dict[str, Any]],
    field: str,
    label: str,
) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for value in values:
        identifier = value.get(field)
        if not isinstance(identifier, str) or not identifier:
            raise DatasetError(f"Every {label} requires {field}")
        if identifier in selected:
            raise DatasetError(f"Duplicate {label} id: {identifier}")
        selected[identifier] = value
    return selected


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise DatasetError(f"Cannot read Judge asset {path}: {exc}") from exc


def _score_failure_rate(items: list[dict[str, Any]], dimension: str) -> float:
    applicable = [
        item["verdict"]["scores"][dimension]
        for item in items
        if item["verdict"]["scores"].get(dimension) is not None
    ]
    return _rate(sum(score < 4 for score in applicable), len(applicable))


def _score_success_rate(items: list[dict[str, Any]], dimension: str) -> float:
    values = [item["verdict"]["scores"][dimension] for item in items]
    return _rate(sum(score >= 4 for score in values), len(values))


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _mean(values: list[int]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return round(ordered[index], 3)


def _packet_usage(
    merged: list[dict[str, Any]],
) -> dict[str, int | bool | None] | None:
    usages = [
        item["packet"]["evidence"].get("token_usage")
        for item in merged
        if item["packet"]["evidence"].get("token_usage") is not None
    ]
    if not usages:
        return None
    aggregate: dict[str, int | bool | None] = {}
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        complete = len(usages) == len(merged) and all(
            usage.get(field) is not None for usage in usages
        )
        aggregate[field] = (
            sum(int(usage[field]) for usage in usages) if complete else None
        )
    aggregate["partial"] = len(usages) != len(merged) or any(
        bool(usage.get("partial")) for usage in usages
    )
    return aggregate


def _judge_summary_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# MyAgent resume evaluation",
        "",
        f"- Judge: {summary['judge']}",
        f"- Result: **{summary['passed']}/{summary['total']} passed**",
        f"- Pass@k: {summary['pass_at_k_rate']}",
        f"- Pass^k: {summary['pass_power_k_rate']}",
        f"- Judge/Verifier disagreements: "
        f"{len(summary['judge_verifier_disagreements'])}",
        f"- Average tool calls: {summary['efficiency']['average_tool_calls']}",
        f"- Component missing-tool rate: "
        f"{summary['component']['missing_required_tool_rate']}",
        f"- Component wrong-order rate: "
        f"{summary['component']['wrong_order_rate']}",
        f"- Component unnecessary-tool rate: "
        f"{summary['component']['unnecessary_tool_rate']}",
        f"- Integration goal success rate: "
        f"{summary['integration']['goal_success_rate']}",
        f"- Integration constraint violation rate: "
        f"{summary['integration']['constraint_violation_rate']}",
        f"- Integration premature-completion rate: "
        f"{summary['integration']['premature_completion_rate']}",
        "",
        "| Dimension | Mean |",
        "| --- | ---: |",
    ]
    for name, value in summary["dimension_means"].items():
        lines.append(f"| {name} | {value} |")
    lines.extend(
        [
            "",
            "| Case | Attempts | Passed | Pass@k | Pass^k |",
            "| --- | ---: | ---: | --- | --- |",
        ]
    )
    for case_id, result in summary["by_case"].items():
        lines.append(
            f"| {case_id} | {result['attempts']} | "
            f"{result['passed_attempts']} | {result['pass_at_k']} | "
            f"{result['pass_power_k']} |"
        )
    lines.extend(
        [
            "",
            "> Private MyAgent evaluation set; this is not a public benchmark score.",
            "",
        ]
    )
    return "\n".join(lines)
