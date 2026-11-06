"""Per-dimension scores, check failures and resource use for single-agent runs."""

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path


def mean(values):
    return statistics.mean(values) if values else None


def summarize(rows, config):
    groups = defaultdict(list)
    for row in rows:
        groups[row["dimension"]].append(row)
    dimensions = {}
    for dimension, selected in sorted(groups.items()):
        usage = [row["usage"] for row in selected]
        dimensions[dimension] = {
            "unique_cases": len({r["case_id"] for r in selected}), "runs": len(selected),
            "passed": sum(r["passed"] for r in selected),
            "pass_rate": mean([int(r["passed"]) for r in selected]),
            "failed_checks": dict(Counter(name for r in selected for name in r["failures"])),
            "mean_duration_ms": mean([r["duration_ms"] for r in selected]),
            "mean_requests": mean([r["budget"]["requests"] for r in selected]),
            "mean_tool_calls": mean([sum(len(t["calls"]) for t in r["stages"]) for r in selected]),
            "total_tokens": sum(u["total_tokens"] for u in usage)
            if all(u and not u.get("partial") and u.get("total_tokens") is not None for u in usage) else None,
        }
    expected = config["expected_runs"]
    return {"config": config, "completed_runs": len(rows), "expected_runs": expected,
            "complete": len(rows) == expected,
            "interpretation": "mechanics_only" if config["provider"] == "replay" else "model_measurement",
            "live_model_executed": config["provider"] != "replay" and bool(rows),
            "all_passed": bool(rows) and len(rows) == expected and all(r["passed"] for r in rows),
            "dimensions": dimensions,
            "macro_pass_rate": mean([item["pass_rate"] for item in dimensions.values()])}


def write_report(output: Path, rows, config):
    report = summarize(rows, config)
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# BFCL-inspired single-agent evaluation", "", f"Interpretation: **{report['interpretation']}**.", "",
             f"Completed: {len(rows)}/{config['expected_runs']}; dataset SHA256: `{config['dataset_sha256']}`.", "",
             "| Dimension | Unique cases | Runs | Passed | Rate |", "|---|---:|---:|---:|---:|"]
    for dimension, stats in report["dimensions"].items():
        lines.append(f"| {dimension} | {stats['unique_cases']} | {stats['runs']} | {stats['passed']} | {stats['pass_rate']:.1%} |")
    lines.extend(["", "## Failed checks", "", "| Dimension | Check | Count |", "|---|---|---:|"])
    for dimension, stats in report["dimensions"].items():
        for check, count in sorted(stats["failed_checks"].items()):
            lines.append(f"| {dimension} | {check} | {count} |")
    lines.extend(["", "runs.jsonl contains tool traces, errors, file states, budgets and model usage."])
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
