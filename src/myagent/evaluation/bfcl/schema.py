"""Versioned benchmark contract; each case belongs to exactly one dimension."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path, PurePosixPath, PureWindowsPath

TOOL_DIMENSIONS = (
    "tool_selection", "argument_accuracy", "call_order", "parallel_calls",
    "tool_restraint", "irrelevance", "missing_parameters", "error_recovery",
)
INTEGRATION_DIMENSIONS = (
    "goal_achievement", "constraint_compliance", "completion_honesty",
)
DIMENSIONS = TOOL_DIMENSIONS + INTEGRATION_DIMENSIONS
TOOLS = ("read_file", "write_file", "edit_file", "glob", "grep", "run_acceptance_tests")
DEFAULT_DATASET = Path(__file__).resolve().parents[4] / "evaluations/bfcl-local/cases.jsonl"


def safe_path(value: str) -> bool:
    return (isinstance(value, str) and bool(value) and "\\" not in value
            and not PurePosixPath(value).is_absolute()
            and not PureWindowsPath(value).drive
            and not set(PurePosixPath(value).parts) & {"..", ".myagent", ".git"})


def validate_cases(cases: list[dict], *, complete: bool = True) -> dict:
    ids, prompts = set(), set()
    counts = Counter()
    families = {dimension: set() for dimension in DIMENSIONS}
    for case in cases:
        if case.get("schema_version") != 1 or case.get("dimension") not in DIMENSIONS:
            raise ValueError("Unsupported benchmark schema or dimension")
        identifier = case.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in ids:
            raise ValueError(f"Duplicate or invalid case id: {identifier}")
        ids.add(identifier)
        prompt = case.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or prompt in prompts:
            raise ValueError(f"Duplicate or empty prompt: {identifier}")
        prompts.add(prompt)
        counts[case["dimension"]] += 1
        family = case.get("family")
        if not isinstance(family, str) or not family:
            raise ValueError(f"Missing family: {identifier}")
        families[case["dimension"]].add(family)
        for field in ("fixture", "expected_files", *(["acceptance_files"] if "acceptance_files" in case else [])):
            files = case.get(field)
            if not isinstance(files, dict) or any(
                not safe_path(path) or not isinstance(content, str)
                for path, content in files.items()
            ):
                raise ValueError(f"Invalid {field}: {identifier}")
        for field in ("available_tools", "approved_tools"):
            names = case.get(field)
            if not isinstance(names, list) or not set(names) <= set(TOOLS):
                raise ValueError(f"Invalid {field}: {identifier}")
        if not set(case["approved_tools"]) <= {"write_file", "edit_file"}:
            raise ValueError(f"Invalid approval policy: {identifier}")
        if case.get("expected_status") not in {"completed", "blocked", "needs_input"}:
            raise ValueError(f"Invalid expected status: {identifier}")
        if not isinstance(case.get("answer"), str):
            raise ValueError(f"Missing expected answer: {identifier}")
        calls = case.get("calls")
        if not isinstance(calls, list):
            raise ValueError(f"Missing call oracle: {identifier}")
        call_ids = [call.get("id") for call in calls]
        if len(set(call_ids)) != len(call_ids) or any(not isinstance(i, str) for i in call_ids):
            raise ValueError(f"Invalid call ids: {identifier}")
        for call in calls:
            if (call.get("name") not in case["available_tools"]
                    or not isinstance(call.get("arguments"), dict)
                    or not isinstance(call.get("ok", True), bool)):
                raise ValueError(f"Invalid call oracle: {identifier}")
        positions = {value: i for i, value in enumerate(call_ids)}
        for edge in case.get("edges", []):
            if (len(edge) != 2 or any(i not in positions for i in edge)
                    or positions[edge[0]] >= positions[edge[1]]):
                raise ValueError(f"Cyclic/invalid dependency: {identifier}")
        for group in case.get("parallel_groups", []):
            if len(group) < 2 or len(set(group)) != len(group) or not set(group) <= set(call_ids):
                raise ValueError(f"Invalid parallel group: {identifier}")
        if not isinstance(case.get("max_calls"), int) or case["max_calls"] < len(calls):
            raise ValueError(f"Invalid call budget: {identifier}")
    if complete:
        if any(counts[d] != 30 for d in DIMENSIONS):
            raise ValueError(f"Exactly 30 unique cases per dimension required: {dict(counts)}")
        if any(len(families[d]) < 6 for d in DIMENSIONS):
            raise ValueError("At least six task families per dimension required")
    return {"cases": len(cases), "dimensions": dict(counts),
            "families": {key: len(value) for key, value in families.items()}}


def load_cases(path: Path = DEFAULT_DATASET) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    validate_cases(cases)
    return cases


def dataset_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def implementation_hash() -> str:
    digest = hashlib.sha256()
    # Include production runtime and adapters: HEAD alone misses uncommitted code.
    root = Path(__file__).resolve().parents[2]
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()
