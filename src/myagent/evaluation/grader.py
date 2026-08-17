"""Hidden acceptance-test execution for repository evaluation cases."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..tooling import FunctionTool


MAX_CAPTURE_CHARS = 12_000


@dataclass(frozen=True)
class GraderResult:
    passed: bool
    exit_code: int | None
    timed_out: bool
    duration_ms: float
    stdout: str
    stderr: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_hidden_tests(
    workspace: Path,
    grader_dir: Path,
    *,
    timeout_seconds: int,
) -> GraderResult:
    """Run trusted hidden unittest files against one temporary workspace.

    The subprocess isolation is for repeatability, not a security sandbox. The caller
    must require explicit consent before executing model-modified Python code.
    """
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    python_paths = [str(workspace / "src"), str(workspace)]
    existing = environment.get("PYTHONPATH")
    if existing:
        python_paths.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(python_paths)
    command = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        str(grader_dir),
        "-p",
        "test_*.py",
        "-v",
    ]
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        return GraderResult(
            passed=False,
            exit_code=None,
            timed_out=True,
            duration_ms=duration_ms,
            stdout=_sanitize_output(exc.stdout or "", workspace, grader_dir),
            stderr=_sanitize_output(exc.stderr or "", workspace, grader_dir),
        )

    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    return GraderResult(
        passed=completed.returncode == 0,
        exit_code=completed.returncode,
        timed_out=False,
        duration_ms=duration_ms,
        stdout=_sanitize_output(completed.stdout, workspace, grader_dir),
        stderr=_sanitize_output(completed.stderr, workspace, grader_dir),
    )


def build_acceptance_tool(
    workspace: Path,
    grader_dir: Path,
    *,
    timeout_seconds: int,
    results: list[GraderResult],
) -> FunctionTool:
    """Build a no-argument tool that runs only this case's fixed hidden grader."""

    def run_acceptance_tests() -> dict[str, Any]:
        result = run_hidden_tests(
            workspace,
            grader_dir,
            timeout_seconds=timeout_seconds,
        )
        results.append(result)
        return {"ok": result.passed, **result.as_dict()}

    return FunctionTool(
        name="run_acceptance_tests",
        description=(
            "Run the fixed acceptance tests for this evaluation task. The command and "
            "hidden test files cannot be changed through this tool."
        ),
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=run_acceptance_tests,
        strict=True,
    )


def _sanitize_output(text: str | bytes, workspace: Path, grader_dir: Path) -> str:
    if isinstance(text, bytes):
        selected = text.decode("utf-8", errors="replace")
    else:
        selected = text
    selected = selected.replace(str(workspace), "<workspace>")
    selected = selected.replace(str(grader_dir), "<hidden-grader>")
    if len(selected) > MAX_CAPTURE_CHARS:
        selected = "...[truncated]...\n" + selected[-MAX_CAPTURE_CHARS:]
    return selected
