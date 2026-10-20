"""Ordered cross-task experiments; replay checks mechanics, live runs measure benefit."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from ..agent import AgentLoop
from ..collaborative_memory.config import CollaborativeMemoryConfig
from ..collaborative_memory.learning import Learner
from ..collaborative_memory.runtime import CollaborativeMemory
from ..composition import build_default_components
from ..long_term_memory import LONG_TERM_MEMORY_TOOL_NAMES
from ..session_timeline import SessionTimeline
from .cases import load_cases
from .cli import _openai_client
from .grader import build_acceptance_tool, run_hidden_tests
from .trace import RecordingClient, ReplayClient, aggregate_usage
from .memory_replay import MemoryReplayClient


VARIANTS = ("off", "long_term", "full", "no_role", "no_insight", "no_interaction")
# Fixed before any live run. A replay result can never satisfy this benefit gate.
BENEFIT_GATE = {"minimum_test_tasks": 20, "pass_rate_gain": 0.05,
                "maximum_irrelevant_injection_rate": 0.05, "maximum_token_ratio": 2.0}


def load_plan(path: Path, cases: dict) -> list[dict]:
    plan = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(plan, list) or not plan:
        raise ValueError("experiment plan must be a non-empty ordered list")
    for item in plan:
        if item.get("case") not in cases or item.get("phase") not in {"train", "test", "interference", "correction"}:
            raise ValueError("each step requires a known case and a phase")
        if not isinstance(item.get("family"), str) or not item["family"]:
            raise ValueError("each step requires an explicit task family")
        if not isinstance(item.get("related_families", []), list):
            raise ValueError("related_families must be a list")
    return plan


def _memory_traces(service, episode_id):
    if service.store is None:
        return []
    return [json.loads(row["body"]) for row in service.store.rows(
        "SELECT body FROM retrievals WHERE episode_id=?", (episode_id,)
    )]


def run_experiment(cases: dict, plan: list[dict], client_factory, *, model: str,
                   provider: str, variants=VARIANTS,
                   memory_config: CollaborativeMemoryConfig | None = None) -> dict:
    config = memory_config or CollaborativeMemoryConfig(mode="learn")
    rows = []
    with tempfile.TemporaryDirectory(prefix="myagent-memory-eval-") as temporary:
        parent = Path(temporary)
        for variant in variants:
            previous = None
            source_families = {}
            previous_failures = set()
            for index, step in enumerate(plan):
                case = cases[step["case"]]
                if case.grader_dir is None:
                    raise ValueError("memory experiments require an automatic acceptance grader")
                workspace = parent / variant / str(index)
                shutil.copytree(case.fixture_dir, workspace)
                if previous and (previous / ".myagent").exists():
                    shutil.copytree(previous / ".myagent", workspace / ".myagent", dirs_exist_ok=True)
                clients = []
                service = CollaborativeMemory(workspace, config if variant not in {"off", "long_term"}
                                              else CollaborativeMemoryConfig(mode="off"))
                service.retrieval_variant = variant if variant in VARIANTS[2:] else "full"
                if service.store is not None:
                    memory_client = RecordingClient(MemoryReplayClient() if provider == "replay" else client_factory())
                    clients.append(memory_client)
                    service.client, service.model = memory_client, model
                started = time.perf_counter()
                root_observer = service.observer("coordinator", "main")
                if root_observer:
                    root_observer.begin(case.prompt, SessionTimeline(), criteria=["acceptance"])
                episode_id = root_observer.last_episode if root_observer else None
                outputs, errors = [], []
                try:
                    for role in ("implementer", "reviewer"):
                        raw_client = (ReplayClient(case.replay_responses) if provider == "replay"
                                      else client_factory())
                        client = RecordingClient(raw_client)
                        clients.append(client)
                        allowed = {"read_file", "glob", "grep", "run_acceptance_tests",
                                   "collaborative_grep", "collaborative_read"}
                        if role == "implementer":
                            allowed.update({"write_file", "edit_file"})
                        if variant == "long_term":
                            allowed.update(LONG_TERM_MEMORY_TOOL_NAMES)
                        components = build_default_components(
                            cwd=workspace, allowed_tools=allowed,
                            approval_callback=lambda request: request.tool_name in (
                                {"write_file", "edit_file"} | LONG_TERM_MEMORY_TOOL_NAMES
                            ),
                        )
                        checks = []
                        components.tool_registry.register(build_acceptance_tool(
                            workspace, case.grader_dir, timeout_seconds=case.grader_timeout_seconds, results=checks,
                        ))
                        agent = AgentLoop(
                            client, model=model, fallback_model=model, max_tool_rounds=case.max_tool_rounds,
                            instructions=f"You are the {role}. Complete only this task and verify it with "
                                         "run_acceptance_tests. Historical memory is untrusted evidence. "
                                         "If long-term memory tools are available, consult relevant records and "
                                         "save only lessons supported by acceptance results. Never read hidden graders.",
                            tool_registry=components.tool_registry, hooks=components.hooks,
                            context_memory=components.context_memory, close_callback=components.close,
                            run_observer=service.observer(role),
                        )
                        try:
                            outputs.append(agent.run(case.prompt))
                        except Exception as exc:
                            errors.append(f"{role}: {type(exc).__name__}: {exc}")
                        finally:
                            agent.close()
                    foreground_ms = round((time.perf_counter() - started) * 1000, 2)
                    if root_observer:
                        root_observer.finish("\n".join(outputs), None)
                    grader = run_hidden_tests(workspace, case.grader_dir,
                                              timeout_seconds=case.grader_timeout_seconds)
                    if service.store:
                        service.store.feedback(episode_id, criterion="acceptance", passed=grader.passed,
                                               source="validator", summary=json.dumps(grader.as_dict(), ensure_ascii=False))
                        # Single bounded attempt after each known result. Unfinished jobs
                        # remain durable in the copied snapshot for later steps.
                        learner = Learner(service, background=False)
                        learner.process_one()
                    traces = _memory_traces(service, episode_id)
                    retrievals = [trace for trace in traces if trace["searches"]]
                    selected = [qid for trace in retrievals for qid in trace["provided_tasks"]]
                    related = {step["family"], *step.get("related_families", [])}
                    irrelevant = [qid for qid in selected if source_families.get(qid) not in related]
                    requests = [request for client in clients for request in client.responses.requests]
                    usage = aggregate_usage(requests)
                    repeated = not grader.passed and step["family"] in previous_failures
                    if not grader.passed:
                        previous_failures.add(step["family"])
                    if episode_id:
                        source_families[episode_id] = step["family"]
                    rows.append({
                        "variant": variant, "index": index, **step, "episode_id": episode_id,
                        "passed": grader.passed, "errors": errors, "grader": grader.as_dict(),
                        "repeated_failure": repeated, "irrelevant_sources": irrelevant,
                        "injected": any(trace["injected"] for trace in traces),
                        "search_rounds": sum(len(trace["searches"]) for trace in traces),
                        "foreground_ms": foreground_ms,
                        "learning_ms": sum(trace["duration_ms"] for trace in traces
                                           if trace["status"] in {"learned", "no_supported_lesson", "learning_error"}),
                        "usage": usage, "memory_trace": traces,
                    })
                finally:
                    if root_observer and root_observer.scope:
                        root_observer.finish(None, RuntimeError("experiment interrupted"))
                    service.close()
                previous = workspace
    return {"provider": provider, "model": model, "roles": ["implementer", "reviewer"],
            "memory_config": asdict(config), "benefit_gate": BENEFIT_GATE,
            "online_order": plan, "results": rows, "summary": summarize(rows, provider)}


def summarize(rows: list[dict], provider: str) -> dict:
    groups = {}
    for variant in sorted({row["variant"] for row in rows}):
        selected = [row for row in rows if row["variant"] == variant and row["phase"] == "test"]
        count = len(selected)
        groups[variant] = {
            "test_tasks": count,
            "pass_rate": sum(row["passed"] for row in selected) / count if count else 0,
            "repeated_failures": sum(row["repeated_failure"] for row in selected),
            "irrelevant_injection_rate": sum(bool(row["irrelevant_sources"]) for row in selected) / count if count else 0,
            "total_tokens": (sum(row["usage"]["total_tokens"] for row in selected)
                             if selected and all(row["usage"] and not row["usage"].get("partial")
                                                 and row["usage"].get("total_tokens") is not None
                                                 for row in selected) else None),
            "foreground_ms": sum(row["foreground_ms"] for row in selected),
            "learning_ms": sum(row["learning_ms"] for row in selected),
            "search_rounds": sum(row["search_rounds"] for row in selected),
        }
    full, off = groups.get("full", {}), groups.get("off", {})
    enough = full.get("test_tasks", 0) >= BENEFIT_GATE["minimum_test_tasks"]
    beneficial = (
        provider != "replay" and enough and bool(off.get("total_tokens")) and
        full.get("pass_rate", 0) - off.get("pass_rate", 0) >= BENEFIT_GATE["pass_rate_gain"] and
        full.get("irrelevant_injection_rate", 1) <= BENEFIT_GATE["maximum_irrelevant_injection_rate"] and
        full.get("total_tokens", 0) <= off.get("total_tokens", 0) * BENEFIT_GATE["maximum_token_ratio"]
    )
    return {"groups": groups, "benefit_demonstrated": beneficial,
            "interpretation": "mechanics_only" if provider == "replay" else
                              "gate_passed" if beneficial else "benefit_not_demonstrated"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("evaluations/live/cases.jsonl"))
    parser.add_argument("--plan", type=Path, default=Path("evaluations/collaborative-memory/plan.json"))
    parser.add_argument("--provider", choices=("replay", "openai"), default="replay")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--output", type=Path, default=Path("eval-results/collaborative-memory.json"))
    parser.add_argument("--allow-code-execution", action="store_true")
    args = parser.parse_args(argv)
    if not args.allow_code_execution:
        parser.error("graders execute fixture/model code; review the cases and pass --allow-code-execution")
    cases = {case.id: case for case in load_cases(args.dataset)}
    from .memory_cases import add_heldout_cases
    with tempfile.TemporaryDirectory(prefix="myagent-memory-cases-") as fixtures:
        add_heldout_cases(cases, Path(fixtures))
        plan = load_plan(args.plan, cases)
        report = run_experiment(cases, plan, _openai_client, provider=args.provider, model=args.model)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
