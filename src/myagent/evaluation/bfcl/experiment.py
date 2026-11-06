"""Execute one single-agent task in a fresh workspace and record its verdict."""

from __future__ import annotations

import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from ..trace import aggregate_usage
from .replay import replay_factory
from .runtime import Budget, Stage
from .schema import INTEGRATION_DIMENSIONS
from .scoring import score, snapshot


def run_case(case, *, provider="replay", model="offline", attempt=1,
             client_factory=None, request_limit=24, output_tokens=2048, tool_limit=64):
    if request_limit < 6 or output_tokens < 1 or tool_limit < 1:
        raise ValueError("Request budget must be >= 6; token and tool limits must be positive")
    started = time.perf_counter()
    budget = Budget(request_limit, output_tokens, tool_limit)
    errors, final = [], ""
    with tempfile.TemporaryDirectory(prefix="myagent-bfcl-") as temporary:
        workspace = Path(temporary)
        for relative, content in case["fixture"].items():
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="")
        raw = replay_factory(case) if provider == "replay" else client_factory()
        stage = None
        try:
            stage = Stage(case, workspace, raw, budget, model=model, request_limit=request_limit)
            try:
                final = stage.agent.run(case["prompt"])
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
            trace = stage.trace()
            if provider == "replay" and raw.responses.consumed != raw.responses.total:
                errors.append("replay_not_fully_consumed")
            files = snapshot(workspace)
        finally:
            try:
                if stage is not None:
                    stage.agent.close()
            finally:
                close = getattr(raw, "close", None)
                if close:
                    close()
    graded = score(case, trace["calls"], final, files, protocol_ok=trace["protocol_ok"],
                   errors=errors, integration=case["dimension"] in INTEGRATION_DIMENSIONS)
    return {"case_id": case["id"], "dimension": case["dimension"], "family": case["family"],
            "variant": "single", "attempt": attempt, "provider": provider, "model": model,
            "interpretation": "mechanics_only" if provider == "replay" else "model_measurement",
            **graded, "final_text": final, "errors": errors, "stages": [trace],
            "files": files, "budget": asdict(budget), "usage": aggregate_usage(trace["requests"]),
            "cost_usd": None, "duration_ms": round((time.perf_counter() - started) * 1000, 3)}
