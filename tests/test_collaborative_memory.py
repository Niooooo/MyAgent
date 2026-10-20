"""Behavioral coverage of task boundaries, evidence, retrieval and recovery."""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from myagent.agent import AgentLoop
from myagent.collaborative_memory.config import CollaborativeMemoryConfig
from myagent.collaborative_memory.management import manage
from myagent.collaborative_memory.retrieval import CorpusSearch, RetrievalTrace, Retriever
from myagent.collaborative_memory.runtime import CollaborativeMemory
from myagent.collaborative_memory.storage import encode, project_identity
from myagent.collaborative_memory.store import MemoryStore
from myagent.composition import AgentConfig, create_default_agent
from myagent.session_timeline import SessionTimeline
from myagent.tooling import ToolRegistry
from tests.fakes import FakeResponses, function_call, response


class MemoryTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = MemoryStore(self.root)
        self.addCleanup(self.store.close)

    def completed(self, *, task="Fix retry timeout", passed=True, roles=None):
        qid = self.store.begin(task, ["tests", "review"])
        event = self.store.event(qid, "attempt", "assistant", "Retry the timeout with a bounded delay.",
                                 agent_id="worker", role="implementer")
        self.store.end(qid)
        self.store.feedback(qid, criterion="tests", passed=passed, summary="retry unit cases checked")
        self.store.feedback(qid, criterion="review", passed=True, summary="review confirmed bounded delay")
        verification = self.store.rows("SELECT event_id FROM evidence WHERE episode_id=?", (qid,))[0]["event_id"]
        proposal = {"text": "Bound the retry delay and verify timeout cases.",
                    "conditions": "retry timeout", "roles": roles or ["implementer"],
                    "event_ids": [event, verification]}
        return qid, proposal

    def test_outcome_requires_all_criteria_and_explicit_evidence(self):
        qid = self.store.begin("fix", ["test", "review"])
        self.store.start_run(qid, "run", "main", "coordinator", None, None, str(self.root))
        self.store.event(qid, "claim", "assistant", "All done successfully")
        self.store.finish_run(qid, "run", root=True, interrupted=False)
        self.assertEqual("unknown", self.store.episode(qid)["outcome"])
        self.store.feedback(qid, criterion="test", passed=True, summary="one test passed", evidence_id="one")
        self.assertEqual("unknown", self.store.episode(qid)["outcome"])
        self.store.feedback(qid, criterion="test", passed=True, summary="one test passed", evidence_id="one")
        self.assertEqual(1, len(self.store.rows("SELECT * FROM evidence")))
        self.store.feedback(qid, criterion="review", passed=True, summary="all conditions reviewed")
        self.assertEqual("success", self.store.episode(qid)["outcome"])
        self.store.feedback(qid, criterion="review", passed=False, summary="missing error handling")
        self.assertEqual("failed", self.store.episode(qid)["outcome"])
        with self.assertRaises(ValueError):
            self.store.feedback(qid, criterion="test", passed=True, summary="model says so", source="model")

    def test_waiting_and_cancel_do_not_create_learning_from_stop(self):
        qid = self.store.begin("fork", ["test"])
        self.store.start_run(qid, "root", "main", "coordinator", None, None, str(self.root))
        wid = self.store.reserve(qid, "root", "fork-1")
        self.store.finish_run(qid, "root", root=True, interrupted=False)
        self.assertEqual("waiting", self.store.episode(qid)["lifecycle"])
        with self.assertRaises(ValueError):
            self.store.feedback(qid, criterion="test", passed=True, summary="premature")
        self.store.end(qid, cancelled=True)
        self.store.event(qid, "late", "assistant", "late child output", agent_id="fork")
        self.store.finish_work(wid)
        self.assertEqual("interrupted", self.store.episode(qid)["lifecycle"])
        self.assertEqual("unknown", self.store.episode(qid)["outcome"])
        self.assertEqual([], self.store.rows("SELECT * FROM jobs"))

    def test_replay_concurrency_blobs_and_projection_rebuild(self):
        qid = self.store.begin("concurrent timeout", [])
        def write(index):
            return self.store.event(qid, f"key:{index % 8}", "tool_result", "timeout " * 1000,
                                    run_id=f"run:{index % 2}", agent_id=str(index % 2))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(80)))
        events = self.store.rows("SELECT * FROM events")
        self.assertEqual(8, len(events))
        self.assertEqual("timeout " * 1000, self.store.event_body(events[0]))
        self.store.rebuild_projections()
        projection = self.store.path("projections/" + qid + ".txt")
        projection.unlink()
        self.store.rebuild_projections()
        self.assertTrue(projection.exists())
        for edge in self.store.rows("SELECT * FROM edges WHERE kind='sequence'"):
            left = self.store.rows("SELECT run_id FROM events WHERE id=?", (edge["source"],))[0]
            right = self.store.rows("SELECT run_id FROM events WHERE id=?", (edge["target"],))[0]
            self.assertEqual(left, right)

    def test_learning_requires_evidence_and_preserves_failure_and_versions(self):
        qid, proposal = self.completed(passed=False)
        version = self.store.episode(qid)["version"]
        iid = self.store.learn(qid, version, [proposal])[0]
        self.assertEqual([iid], self.store.learn(qid, version, [proposal]))
        self.assertEqual("failed", self.store.insight(iid)["sources"][0]["polarity"])
        self.store.change_insight(iid, "revise", reason="use lower bound", text="Limit retries to three attempts.")
        self.assertEqual(2, self.store.insight(iid)["version"])
        self.assertEqual(2, len(self.store.rows("SELECT * FROM insight_versions")))
        self.assertEqual([], self.store.learn(qid, version, [proposal]))
        unknown = self.store.begin("unknown", [])
        self.store.end(unknown)
        with self.assertRaises(ValueError):
            self.store.learn(unknown, 1, [proposal])

    def test_disable_delete_cannot_resurrect_and_shared_sources_survive(self):
        qid, proposal = self.completed()
        iid = self.store.learn(qid, self.store.episode(qid)["version"], [proposal])[0]
        other, proposal2 = self.completed()
        self.store.learn(other, self.store.episode(other)["version"], [proposal2])
        self.store.delete_episode(qid)
        self.assertEqual(other, self.store.insight(iid)["sources"][0]["episode_id"])
        self.store.change_insight(iid, "disable", reason="incorrect")
        with self.assertRaises(ValueError):
            self.store.projection_record(iid)
        self.assertEqual([], self.store.learn(other, self.store.episode(other)["version"], [proposal2]))
        self.store.change_insight(iid, "delete", reason="removed")
        self.assertEqual([], self.store.learn(other, self.store.episode(other)["version"], [proposal2]))
        self.assertFalse(self.store.path("projections/" + iid + ".txt").exists())
        self.store.delete_episode(other)
        self.assertEqual([], list(self.store.path("blobs").glob("*.txt")))

    def test_agentic_rewrite_role_packages_and_restart(self):
        qid, proposal = self.completed()
        self.store.learn(qid, self.store.episode(qid)["version"], [proposal])
        reviewer = dict(proposal, text="Review retry boundary assertions.", roles=["reviewer"])
        self.store.learn(qid, self.store.episode(qid)["version"], [reviewer])
        current = self.store.begin("retry timeout again", [])
        reopened = MemoryStore(self.root)
        self.addCleanup(reopened.close)
        def query(role):
            responses = FakeResponses([
                response([function_call("miss", "collaborative_grep", encode({"terms": ["absent_symbol"]}))]),
                response([function_call("hit", "collaborative_grep", encode({"terms": ["retry timeout"]}))]),
                response([], encode({"seeds": [{"episode_id": qid, "reason": "same retry timeout boundary"}]})),
            ])
            return Retriever(reopened, CollaborativeMemoryConfig(mode="learn"), SimpleNamespace(responses=responses),
                             "fake").retrieve(current, "retry timeout", role, [])
        implementation, trace = query("implementer")
        review, _ = query("reviewer")
        self.assertIn("Bound the retry delay", implementation)
        self.assertNotIn("Review retry boundary", implementation)
        self.assertIn("Review retry boundary", review)
        self.assertNotIn("Bound the retry delay", review)
        self.assertIn(qid, implementation)
        self.assertIn("sources=", implementation)
        self.assertEqual(2, len(trace.searches))
        self.assertEqual(3, trace.model_calls)
        self.assertLessEqual(len(implementation), 6000)
        self.assertLessEqual(trace.read_chars, 16000)
        self.assertEqual({"retrieved", "provided"}, {row["stage"] for row in self.store.rows("SELECT * FROM usage")})

    def test_retrieval_limits_permissions_unrelated_and_corruption(self):
        qid, proposal = self.completed()
        iid = self.store.learn(qid, self.store.episode(qid)["version"], [proposal])[0]
        current = self.store.begin("unrelated", [])
        self.store.rebuild_projections()
        config = CollaborativeMemoryConfig(mode="learn", search_rounds=1, hits_per_round=2, read_chars=15)
        trace = RetrievalTrace()
        search = CorpusSearch(self.store, config, time.monotonic() + 10, trace, current)
        result = search.grep(["timeout"])
        self.assertLessEqual(trace.read_chars, 15)
        self.assertLessEqual(len(result["hits"]), 2)
        self.assertFalse(search.grep(["retry"])["ok"])
        self.assertFalse(search.read("../../secret")["ok"])
        self.assertEqual([], search.registry(frozenset()).definitions)
        self.store.path("projections/" + iid + ".txt").write_text("corrupt", encoding="utf-8")
        bad = CorpusSearch(self.store, replace(config, read_chars=5000), time.monotonic() + 10,
                           RetrievalTrace(), current)
        bad.grep(["timeout"])
        self.assertIn("corrupt", bad.trace.error)
        client = SimpleNamespace(responses=FakeResponses([response([], '{"seeds":[]}')]))
        material, trace = Retriever(self.store, config, client, "fake").retrieve(current, "gardening", "reviewer", [])
        self.assertEqual("", material)
        client = SimpleNamespace(responses=FakeResponses([TimeoutError("expired")]))
        material, trace = Retriever(self.store, config, client, "fake").retrieve(current, "retry", "reviewer", [])
        self.assertEqual("", material)
        self.assertEqual("timeout", trace.status)

    def test_explicit_continuation_and_recovery(self):
        qid, _ = self.completed()
        self.store.begin("continue", [], episode_id=qid)
        self.assertEqual("unknown", self.store.episode(qid)["outcome"])
        other = self.store.begin("continue", [])
        self.assertNotEqual(qid, other)
        self.store.reserve(qid, "root", "pending")
        self.store.recover_interrupted()
        self.assertEqual("interrupted", self.store.episode(qid)["lifecycle"])
        self.assertEqual("unknown", self.store.episode(qid)["outcome"])

    def test_counterexamples_apply_only_to_explicit_citations_and_are_idempotent(self):
        original, proposal = self.completed()
        iid = self.store.learn(original, self.store.episode(original)["version"], [proposal])[0]
        later, _ = self.completed(passed=False, task="New retry deployment")
        evidence = self.store.rows("SELECT id FROM evidence WHERE episode_id=? AND passed=0", (later,))[0]["id"]
        update = {"insight_id": iid, "evidence_id": evidence, "action": "revise",
                  "reason": "new acceptance failure contradicts the retry constant",
                  "text": "Read the current policy before choosing a retry delay."}
        self.store.use(later, iid, "provided")
        with self.assertRaisesRegex(ValueError, "injection alone"):
            self.store.apply_insight_feedback(later, [update])
        self.store.use(later, iid, "cited")
        self.store.apply_insight_feedback(later, [update])
        self.store.apply_insight_feedback(later, [update])
        self.assertEqual(2, self.store.insight(iid)["version"])
        self.assertTrue(self.store.rows("SELECT * FROM edges WHERE source=? AND kind='counterexample'", (later,)))


class RuntimeMemoryTests(unittest.TestCase):
    def test_off_creates_no_memory_and_record_captures_observable_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for mode in ("off", "record"):
                client = SimpleNamespace(responses=FakeResponses([
                    response([SimpleNamespace(type="reasoning", summary="secret thinking")], "done"),
                ]))
                agent = create_default_agent(client, cwd=root,
                                             config=AgentConfig(collaborative_memory=CollaborativeMemoryConfig(mode=mode)))
                try:
                    self.assertEqual("done", agent.run("task", acceptance_criteria=["review"]))
                    self.assertEqual(1, len(client.responses.requests))
                    if mode == "off":
                        self.assertFalse((root / ".myagent/collaborative-memory").exists())
                    else:
                        store = agent.collaborative_memory.store
                        content = encode(store.rows("SELECT * FROM events"))
                        self.assertIn("done", content)
                        self.assertNotIn("secret thinking", content)
                        self.assertEqual("unknown", store.episode(agent.collaborative_memory.last_episode)["outcome"])
                finally:
                    agent.close()

    def test_fork_reserves_original_episode_and_history_remains_isolated(self):
        with tempfile.TemporaryDirectory() as folder:
            entered, release = threading.Event(), threading.Event()
            class ChildResponses:
                def create(self, **kwargs):
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError("test release")
                    return response([], "fork result")
            main = SimpleNamespace(responses=FakeResponses([
                response([function_call("fork", "fork_subagent", '{"task":"child task"}')]),
                response([], "main returned"),
                response([], "new task done"),
            ]))
            child = SimpleNamespace(responses=ChildResponses())
            agent = create_default_agent(main, subagent_client=child, cwd=folder,
                                         config=AgentConfig(collaborative_memory=CollaborativeMemoryConfig(mode="record")))
            try:
                agent.run("first")
                self.assertTrue(entered.wait(2))
                store = agent.collaborative_memory.store
                original = agent.collaborative_memory.last_episode
                self.assertEqual("waiting", store.episode(original)["lifecycle"])
                agent.run("second")
                second = agent.collaborative_memory.last_episode
                release.set()
                for _ in range(100):
                    if store.episode(original)["lifecycle"] == "ended":
                        break
                    time.sleep(.01)
                self.assertEqual("ended", store.episode(original)["lifecycle"])
                self.assertNotIn("fork result", encode(store.sources(second)))
                self.assertIn("fork result", encode(store.sources(original)))
                runs = store.rows("SELECT * FROM runs WHERE episode_id=?", (original,))
                self.assertEqual(2, len(runs))
                self.assertNotIn("child task", str(main.responses.requests[0]["input"]))
            finally:
                release.set()
                agent.close()

    def test_team_message_consumption_and_member_reuse_keep_original_sources(self):
        with tempfile.TemporaryDirectory() as folder, closing(
            CollaborativeMemory(Path(folder), CollaborativeMemoryConfig(mode="record"))
        ) as service:
            from myagent.agent_team import AgentTeamManager
            from myagent.filesystem import WorkspaceRoot
            def factory(name, role, inbox):
                client = SimpleNamespace(responses=FakeResponses([response([], "first result"), response([], "second result")]))
                return (AgentLoop(client, tool_registry=ToolRegistry(), inbox_reader=inbox,
                                  run_observer=service.observer(role, name)), WorkspaceRoot(folder))
            team = AgentTeamManager(folder, factory, memory_runtime=service)
            self.addCleanup(team.close)
            self.assertTrue(team.create_teammate("reviewer", "reviewer")["ok"])
            original = service.observer("coordinator", "main")
            original.begin("task A", SessionTimeline())
            qid = original.last_episode
            team.run_teammate("reviewer", "review A")
            original.finish("returned", None)
            for _ in range(100):
                if service.store.episode(qid)["lifecycle"] == "ended":
                    break
                time.sleep(.01)
            second = service.observer("coordinator", "main")
            second.begin("task B", SessionTimeline())
            batch = team.inbox_reader("main")()
            self.assertIsNotNone(batch)
            batch.acknowledge()
            self.assertIsNone(team.inbox_reader("main")())
            team.run_teammate("reviewer", "review B")
            second.finish("returned", None)
            team.close()
            self.assertIn("first result", encode(service.store.sources(qid)))
            self.assertNotIn("first result", encode(service.store.sources(second.last_episode)))
            self.assertIn("second result", encode(service.store.sources(second.last_episode)))
            self.assertTrue(service.store.rows("SELECT * FROM edges WHERE kind='delivery'"))

    def test_learning_queue_restart_uses_validation_references(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MemoryStore(Path(folder))
            qid = store.begin("repair timeout", ["tests"])
            store.event(qid, "attempt", "assistant", "Added bounded retry", role="implementer")
            store.end(qid)
            store.feedback(qid, criterion="tests", passed=True, summary="timeout tests passed")
            store.close()
            class Extractor:
                def create(self, **kwargs):
                    payload = json.loads(kwargs["input"][0]["content"])
                    refs = [e["id"] for e in payload["events"]]
                    return response([], encode({"insights": [{
                        "text": "Bound retries before testing timeout behavior.", "conditions": "timeout",
                        "roles": ["implementer"], "event_ids": refs,
                    }]}))
            feedback_started, release_feedback = threading.Event(), threading.Event()
            apply_feedback = MemoryStore.apply_insight_feedback

            def paused_feedback(memory_store, *args, **kwargs):
                feedback_started.set()
                if not release_feedback.wait(5):
                    raise TimeoutError("test did not release learning feedback")
                return apply_feedback(memory_store, *args, **kwargs)

            with patch.object(MemoryStore, "apply_insight_feedback", paused_feedback):
                service = CollaborativeMemory(Path(folder), CollaborativeMemoryConfig(mode="learn"),
                                              client=SimpleNamespace(responses=Extractor()), model="fake")
                try:
                    self.assertTrue(feedback_started.wait(5))
                    # An insight is visible before feedback and job finalization finish.
                    self.assertEqual(1, len(service.store.rows("SELECT * FROM insights")))
                    query = "SELECT state,error FROM jobs WHERE episode_id=?"
                    job = service.store.rows(query, (qid,))[0]
                    self.assertEqual("running", job["state"])
                    release_feedback.set()
                    deadline = time.monotonic() + 5
                    while job["state"] in {"pending", "running"} and time.monotonic() < deadline:
                        time.sleep(.01)
                        job = service.store.rows(query, (qid,))[0]
                    self.assertEqual("done", job["state"], job)
                    self.assertEqual(1, len(service.store.rows("SELECT * FROM insights")))
                finally:
                    release_feedback.set()
                    service.close()

    def test_worktree_identity_shares_project_but_keeps_execution_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "repo"
            root.mkdir()
            def git(*args):
                return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)
            git("init")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "fixture")
            worktree = Path(folder) / "branch"
            git("worktree", "add", "--detach", str(worktree))
            self.assertEqual(project_identity(root), project_identity(worktree))
            store = MemoryStore(worktree)
            try:
                qid = store.begin("task", [])
                self.assertEqual(str(worktree.resolve()), store.episode(qid)["workspace"])
                self.assertEqual(root.resolve(), store.project)
            finally:
                store.close()

    def test_management_empty_status_is_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            result = manage(Path(folder), "status", {})
            self.assertEqual([], result["episodes"])
            self.assertFalse((Path(folder) / ".myagent").exists())


if __name__ == "__main__":
    unittest.main()
