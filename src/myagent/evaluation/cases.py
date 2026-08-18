"""Load and validate the self-built repository-task evaluation set."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
_CASE_ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_CATEGORIES = frozenset(
    {
        "bugfix",
        "feature",
        "configuration",
        "tool_selection",
        "tool_order",
        "tool_restraint",
    }
)
_TRACKS = frozenset({"component", "integration"})
_APPROVABLE_TOOLS = frozenset({"write_file", "edit_file"})
_TRACE_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "run_acceptance_tests",
    }
)


class DatasetError(ValueError):
    """Raised when an evaluation manifest or referenced asset is invalid."""


@dataclass(frozen=True)
class EvalCase:
    """One isolated repository task and its hidden acceptance grader."""

    schema_version: int
    id: str
    category: str
    track: str
    description: str
    prompt: str
    fixture_dir: Path
    grader_dir: Path | None
    replay_responses: tuple[dict[str, Any], ...]
    max_tool_rounds: int
    grader_timeout_seconds: int
    allowed_approvals: frozenset[str]
    baseline_must_fail: bool
    require_validation_tool: bool
    expected_changed_files: tuple[str, ...]
    required_tools: tuple[str, ...]
    order_constraints: tuple[tuple[str, str], ...]
    forbidden_tools: frozenset[str]
    max_tool_calls: int | None
    success_criteria: tuple[str, ...]
    constraints: tuple[str, ...]


def load_cases(path: str | Path) -> list[EvalCase]:
    """Read a JSONL manifest and validate every case before execution."""
    manifest = Path(path).resolve()
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DatasetError(f"Cannot read evaluation dataset {manifest}: {exc}") from exc

    cases: list[EvalCase] = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(
                f"Invalid JSON in {manifest} line {line_number}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise DatasetError(
                f"Invalid case in {manifest} line {line_number}: expected an object"
            )
        case = _parse_case(payload, manifest.parent, line_number)
        if case.id in seen_ids:
            raise DatasetError(f"Duplicate evaluation case id: {case.id}")
        seen_ids.add(case.id)
        cases.append(case)

    if not cases:
        raise DatasetError(f"Evaluation dataset is empty: {manifest}")
    return cases


def _parse_case(payload: dict[str, Any], root: Path, line_number: int) -> EvalCase:
    context = f"case on line {line_number}"
    schema_version = payload.get("schema_version")
    if schema_version not in {1, SCHEMA_VERSION}:
        raise DatasetError(
            f"{context}: schema_version must be 1 or {SCHEMA_VERSION}"
        )

    case_id = _non_empty_string(payload, "id", context)
    if not _CASE_ID.fullmatch(case_id):
        raise DatasetError(
            f"{context}: id must match {_CASE_ID.pattern!r}, got {case_id!r}"
        )
    category = _non_empty_string(payload, "category", context)
    if category not in _CATEGORIES:
        raise DatasetError(
            f"{context}: category must be one of {sorted(_CATEGORIES)}"
        )

    fixture_dir = _contained_path(
        root,
        _non_empty_string(payload, "fixture", context),
        context,
        "fixture",
    )
    track = payload.get("track", "integration")
    if not isinstance(track, str) or track not in _TRACKS:
        raise DatasetError(f"{context}: track must be one of {sorted(_TRACKS)}")

    grader_value = payload.get("grader")
    grader_dir = (
        None
        if grader_value is None
        else _contained_path(
            root,
            _non_empty_string(payload, "grader", context),
            context,
            "grader",
        )
    )
    replay_value = payload.get("replay_script")
    if replay_value is None:
        replay_responses: tuple[dict[str, Any], ...] = ()
    else:
        replay_path = _contained_path(
            root,
            _non_empty_string(payload, "replay_script", context),
            context,
            "replay_script",
            expect_directory=False,
        )
        replay_responses = _load_replay(replay_path, context)

    max_tool_rounds = _bounded_integer(
        payload,
        "max_tool_rounds",
        context,
        minimum=1,
        maximum=50,
    )
    grader_timeout_seconds = (
        _bounded_integer(
            payload,
            "grader_timeout_seconds",
            context,
            minimum=1,
            maximum=120,
        )
        if "grader_timeout_seconds" in payload
        else 10
    )

    approvals = payload.get("allowed_approvals")
    if not isinstance(approvals, list) or any(
        not isinstance(item, str) or not item for item in approvals
    ):
        raise DatasetError(f"{context}: allowed_approvals must be a list of tool names")
    unknown_approvals = sorted(set(approvals).difference(_APPROVABLE_TOOLS))
    if unknown_approvals:
        raise DatasetError(
            f"{context}: unsupported approval tools: {', '.join(unknown_approvals)}"
        )

    expected_changed_files = payload.get("expected_changed_files")
    if not isinstance(expected_changed_files, list) or (
        schema_version == 1 and not expected_changed_files
    ):
        raise DatasetError(
            f"{context}: expected_changed_files must be a list"
        )
    normalized_changed_files: list[str] = []
    for value in expected_changed_files:
        if not isinstance(value, str) or not value.strip():
            raise DatasetError(
                f"{context}: expected_changed_files must contain non-empty paths"
            )
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise DatasetError(
                f"{context}: expected changed path must stay relative: {value!r}"
            )
        normalized_changed_files.append(candidate.as_posix())
    if len(normalized_changed_files) != len(set(normalized_changed_files)):
        raise DatasetError(f"{context}: expected_changed_files contains duplicates")

    baseline_must_fail = payload.get(
        "baseline_must_fail",
        grader_dir is not None,
    )
    require_validation_tool = payload.get(
        "require_validation_tool",
        grader_dir is not None,
    )
    if not isinstance(baseline_must_fail, bool):
        raise DatasetError(f"{context}: baseline_must_fail must be a boolean")
    if not isinstance(require_validation_tool, bool):
        raise DatasetError(f"{context}: require_validation_tool must be a boolean")
    if grader_dir is None and (baseline_must_fail or require_validation_tool):
        raise DatasetError(
            f"{context}: grader is required for baseline or validation checks"
        )

    required_tools = _tool_list(payload, "required_tools", context)
    forbidden_tools = frozenset(_tool_list(payload, "forbidden_tools", context))
    overlap = sorted(set(required_tools).intersection(forbidden_tools))
    if overlap:
        raise DatasetError(
            f"{context}: tools cannot be both required and forbidden: "
            + ", ".join(overlap)
        )
    order_constraints = _order_constraints(payload, context)
    max_tool_calls_value = payload.get("max_tool_calls")
    max_tool_calls = None
    if max_tool_calls_value is not None:
        max_tool_calls = _bounded_integer(
            payload,
            "max_tool_calls",
            context,
            minimum=0,
            maximum=100,
        )
    success_criteria = _string_list(payload, "success_criteria", context)
    constraints = _string_list(payload, "constraints", context)
    if schema_version == SCHEMA_VERSION and not success_criteria:
        raise DatasetError(f"{context}: success_criteria must not be empty")

    return EvalCase(
        schema_version=schema_version,
        id=case_id,
        category=category,
        track=track,
        description=_non_empty_string(payload, "description", context),
        prompt=_non_empty_string(payload, "prompt", context),
        fixture_dir=fixture_dir,
        grader_dir=grader_dir,
        replay_responses=replay_responses,
        max_tool_rounds=max_tool_rounds,
        grader_timeout_seconds=grader_timeout_seconds,
        allowed_approvals=frozenset(approvals),
        baseline_must_fail=baseline_must_fail,
        require_validation_tool=require_validation_tool,
        expected_changed_files=tuple(normalized_changed_files),
        required_tools=required_tools,
        order_constraints=order_constraints,
        forbidden_tools=forbidden_tools,
        max_tool_calls=max_tool_calls,
        success_criteria=success_criteria,
        constraints=constraints,
    )


def _load_replay(path: Path, context: str) -> tuple[dict[str, Any], ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DatasetError(f"{context}: cannot read replay script {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DatasetError(f"{context}: invalid replay script {path}: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise DatasetError(f"{context}: replay script must be a non-empty JSON array")

    responses: list[dict[str, Any]] = []
    for response_index, response in enumerate(payload, start=1):
        response_context = f"{context}, replay response {response_index}"
        if not isinstance(response, dict):
            raise DatasetError(f"{response_context}: expected an object")
        output = response.get("output")
        output_text = response.get("output_text", "")
        if not isinstance(output, list) or not isinstance(output_text, str):
            raise DatasetError(
                f"{response_context}: output must be a list and output_text a string"
            )
        for item_index, item in enumerate(output, start=1):
            item_context = f"{response_context}, output item {item_index}"
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                raise DatasetError(f"{item_context}: expected an object with type")
            if item["type"] != "function_call":
                continue
            for field in ("call_id", "name"):
                if not isinstance(item.get(field), str) or not item[field]:
                    raise DatasetError(
                        f"{item_context}: function_call requires non-empty {field}"
                    )
            has_object = "arguments" in item
            has_raw = "arguments_raw" in item
            if has_object == has_raw:
                raise DatasetError(
                    f"{item_context}: provide exactly one of arguments or arguments_raw"
                )
            if has_object and not isinstance(item["arguments"], dict):
                raise DatasetError(f"{item_context}: arguments must be an object")
            if has_raw and not isinstance(item["arguments_raw"], str):
                raise DatasetError(f"{item_context}: arguments_raw must be a string")
        responses.append(response)
    return tuple(responses)


def _contained_path(
    root: Path,
    value: str,
    context: str,
    field: str,
    *,
    expect_directory: bool = True,
) -> Path:
    relative = Path(value)
    if relative.is_absolute():
        raise DatasetError(f"{context}: {field} must be relative to the dataset")
    resolved_root = root.resolve()
    resolved = (resolved_root / relative).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise DatasetError(f"{context}: {field} escapes the dataset directory") from exc
    available = resolved.is_dir() if expect_directory else resolved.is_file()
    if not available:
        expected = "directory" if expect_directory else "file"
        raise DatasetError(f"{context}: {field} {expected} does not exist: {resolved}")
    return resolved


def _non_empty_string(payload: dict[str, Any], field: str, context: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"{context}: {field} must be a non-empty string")
    return value.strip()


def _bounded_integer(
    payload: dict[str, Any],
    field: str,
    context: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = payload.get(field)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise DatasetError(
            f"{context}: {field} must be an integer from {minimum} to {maximum}"
        )
    return value


def _string_list(
    payload: dict[str, Any],
    field: str,
    context: str,
) -> tuple[str, ...]:
    value = payload.get(field, [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise DatasetError(f"{context}: {field} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _tool_list(
    payload: dict[str, Any],
    field: str,
    context: str,
) -> tuple[str, ...]:
    selected = _string_list(payload, field, context)
    unknown = sorted(set(selected).difference(_TRACE_TOOLS))
    if unknown:
        raise DatasetError(
            f"{context}: unsupported {field}: {', '.join(unknown)}"
        )
    return selected


def _order_constraints(
    payload: dict[str, Any],
    context: str,
) -> tuple[tuple[str, str], ...]:
    value = payload.get("order_constraints", [])
    if not isinstance(value, list):
        raise DatasetError(f"{context}: order_constraints must be a list")
    constraints: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or any(
                not isinstance(tool, str) or tool not in _TRACE_TOOLS
                for tool in item
            )
        ):
            raise DatasetError(
                f"{context}: each order constraint must contain two supported tools"
            )
        constraints.append((item[0], item[1]))
    return tuple(constraints)
