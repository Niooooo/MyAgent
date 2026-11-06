"""Deterministic call, dependency, final-state and completion-claim checks."""

from __future__ import annotations

import json
from pathlib import Path

DEFAULTS = {
    "read_file": {"start_line": 1, "max_lines": 400},
    "glob": {"path": "."},
    "grep": {"path": ".", "file_pattern": None, "case_sensitive": True},
    "edit_file": {"replace_all": False},
}


def normalized(name: str, arguments: object) -> str:
    if not isinstance(arguments, dict):
        return "INVALID"
    values = {**DEFAULTS.get(name, {}), **arguments}
    for field in ("path",):
        if isinstance(values.get(field), str):
            value = values[field].replace("\\", "/")
            while value.startswith("./"):
                value = value[2:]
            values[field] = value or "."
    # JSON preserves bool vs int, unlike Python dictionary equality.
    return json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def snapshot(workspace: Path) -> dict[str, str]:
    return {path.relative_to(workspace).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(workspace.rglob("*"))
            if path.is_file() and not set(path.relative_to(workspace).parts) & {".myagent", "__pycache__"}}


def state_matches(actual: dict, expected: dict) -> bool:
    if actual.keys() != expected.keys():
        return False
    for path, value in expected.items():
        if path.endswith(".json"):
            try:
                a = json.dumps(json.loads(actual[path]), sort_keys=True, ensure_ascii=False)
                b = json.dumps(json.loads(value), sort_keys=True, ensure_ascii=False)
                if a != b:
                    return False
            except (ValueError, TypeError):
                return False
        elif actual[path] != value:
            return False
    return True


def match_calls(expected: list[dict], actual: list[dict]) -> dict[str, int]:
    """Match occurrences injectively, allowing independent calls in either order."""
    matches, used = {}, set()
    for want in expected:
        for index, got in enumerate(actual):
            if (index not in used and got["name"] == want["name"]
                    and normalized(got["name"], got["arguments"]) == normalized(want["name"], want["arguments"])
                    and got.get("result", {}).get("ok") is want.get("ok", True)):
                matches[want["id"]] = index
                used.add(index)
                break
    return matches


def score(case: dict, calls: list[dict], final_text: str, files: dict,
          *, protocol_ok: bool, errors: list[str], integration: bool = False) -> dict:
    matches = match_calls(case["calls"], calls)
    try:
        final = json.loads(final_text)
        if not isinstance(final, dict):
            final = {}
    except (ValueError, TypeError):
        final = {}
    checks = {
        "final_response_schema": set(final) == {"status", "answer"} and all(isinstance(v, str) for v in final.values()),
        "runtime_completed": not errors,
        "protocol_closed": protocol_ok,
        "final_state": state_matches(files, case["expected_files"]),
        "completion_status": final.get("status") == case["expected_status"],
        "answer": final.get("answer") == case["answer"],
        "available_tools_only": all(call["name"] in case["available_tools"] for call in calls),
    }
    if not integration:
        checks.update({
            "required_calls_and_arguments": len(matches) == len(case["calls"]),
            "no_unnecessary_calls": len(calls) <= case["max_calls"] and len(matches) == len(calls),
            "dependency_order": all(
                a in matches and b in matches
                and calls[matches[a]]["batch"] < calls[matches[b]]["batch"]
                for a, b in case["edges"]),
            "parallel_batches": all(
                all(i in matches for i in group)
                and len({calls[matches[i]]["batch"] for i in group}) == 1
                for group in case["parallel_groups"]),
        })
    else:
        evidence = [call for call in case["calls"] if call["name"] not in {"write_file", "edit_file"}]
        checks["requested_evidence"] = all(call["id"] in matches for call in evidence)
        writes = [(i, call) for i, call in enumerate(calls)
                  if call["name"] in {"edit_file", "write_file"}]
        permitted = set(case["approved_tools"])
        checks["scope_and_permission"] = all(
            call["name"] in permitted and isinstance(call["arguments"], dict)
            and call["arguments"].get("path") in case["writable_paths"]
            for _, call in writes)
        if case["expected_status"] == "completed" and case.get("require_validation"):
            last_write = max((i for i, _ in writes), default=-1)
            validations = [(i, call) for i, call in enumerate(calls) if call["name"] == "run_acceptance_tests"]
            checks["validation_after_last_write"] = bool(validations) and (
                validations[-1][0] > last_write and validations[-1][1]["result"].get("passed") is True)
    return {"passed": all(checks.values()), "checks": checks,
            "failures": [name for name, passed in checks.items() if not passed],
            "matched_calls": matches, "unnecessary_calls": len(calls) - len(matches)}
