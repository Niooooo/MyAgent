"""Application controls, failure handling and permission regression checks."""

import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from myagent import cli
from myagent.agent import AgentLoop
from myagent.collaborative_memory.config import CollaborativeMemoryConfig
from myagent.collaborative_memory.learning import Learner
from myagent.collaborative_memory.retrieval import CorpusSearch, RetrievalTrace, Retriever
from myagent.collaborative_memory.runtime import CollaborativeMemory
from myagent.collaborative_memory.storage import encode
from myagent.collaborative_memory.store import MemoryStore
from myagent.composition import AgentConfig, create_default_agent
from myagent.desktop_sidecar import DesktopSidecar
from myagent.gui import SessionManager
from myagent.gui_models import DesktopSettingsStore, ModelStore
from myagent.session_timeline import SessionTimeline
from myagent.tooling import ToolRegistry
from tests.fakes import FakeResponses, function_call, response


class EntryPointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def episode(self):
        store = MemoryStore(self.root)
        self.addCleanup(store.close)
        qid = store.begin("retry failure", ["acceptance"])
        event = store.event(qid, "work", "assistant", "Limit retry attempts", role="implementer")
        store.end(qid)
        return store, qid, event

    def test_cli_feedback_does_not_construct_a_model(self):
        store, qid, _ = self.episode()
        args = ["myagent", "--memory-action", "feedback", "--memory-episode", qid,
                "--criterion", "acceptance", "--result", "passed", "--evidence", "all assertions passed"]
        with patch.object(cli.Path, "cwd", return_value=self.root), \
             patch.object(cli.sys, "argv", args), patch.object(cli, "create_default_agent") as factory, \
             redirect_stdout(io.StringIO()) as output:
            cli.main()
        factory.assert_not_called()
        self.assertEqual("success", json.loads(output.getvalue())["episode"]["outcome"])
        self.assertEqual("success", store.episode(qid)["outcome"])

    def test_desktop_mode_feedback_sources_and_disable(self):
        store, qid, event = self.episode()
        manager = SessionManager(store=ModelStore(self.root / "models.json"), default_cwd=self.root)
        sidecar = DesktopSidecar(manager, start_polling=False)
        self.addCleanup(sidecar.close)
        session_id = sidecar.snapshot()["activeSessionId"]
        settings = manager.settings.public_dict()
        settings["collaborativeMemoryMode"] = "learn"
        sidecar.dispatch("settings.update", settings)
        self.assertEqual("learn", DesktopSettingsStore(self.root / "settings.json").load().collaborative_memory_mode)
        def command(action, values):
            return sidecar.dispatch("memory.command", {"sessionId": session_id, "action": action, "values": values})["memory"]
        feedback = command("feedback", {"episode_id": qid, "criterion": "acceptance",
                                         "passed": False, "summary": "timeout path failed"})
        self.assertEqual("failed", feedback["episode"]["outcome"])
        sources = command("sources", {"episode_id": qid})
        verification = sources["evidence"][0]["event_id"]
        iid = store.learn(qid, store.episode(qid)["version"], [{
            "text": "Verify timeout before accepting retry changes.", "conditions": "retry failure",
            "roles": ["reviewer"], "event_ids": [event, verification],
        }])[0]
        command("disable", {"insight_id": iid, "reason": "superseded by explicit counterexample"})
        self.assertEqual("disabled", store.insight(iid)["status"])

    def test_model_mutation_requires_permission_and_cannot_assert_user_feedback(self):
        store, qid, event = self.episode()
        store.feedback(qid, criterion="acceptance", passed=True, summary="checked")
        verification = store.sources(qid)["evidence"][0]["event_id"]
        iid = store.learn(qid, store.episode(qid)["version"], [{
            "text": "Bound retry attempts.", "conditions": "retry",
            "roles": ["*"], "event_ids": [event, verification],
        }])[0]
        client = SimpleNamespace(responses=FakeResponses([]))
        agent = create_default_agent(client, cwd=self.root,
                                     config=AgentConfig(collaborative_memory=CollaborativeMemoryConfig(mode="record")))
        self.addCleanup(agent.close)
        result = agent.tool_registry.execute("change_collaborative_insight",
                                             encode({"insight_id": iid, "action": "delete", "reason": "test"}))
        self.assertFalse(result["ok"])
        self.assertEqual("active", store.insight(iid)["status"])
        names = {tool["name"] for tool in agent.tool_registry.definitions}
        self.assertNotIn("feedback", names)

    def test_retry_exhaustion_keeps_durable_job_and_error(self):
        store, qid, _ = self.episode()
        store.feedback(qid, criterion="acceptance", passed=False, summary="verified failing test")
        service = CollaborativeMemory(self.root, CollaborativeMemoryConfig(mode="learn", learning_attempts=1))
        self.addCleanup(service.close)
        service.client = SimpleNamespace(responses=FakeResponses([TimeoutError("extractor unavailable")]))
        learner = Learner(service, background=False)
        with self.assertWarns(RuntimeWarning):
            self.assertTrue(learner.process_one())
        job = store.rows("SELECT * FROM jobs")[0]
        self.assertEqual("failed", job["state"])
        self.assertIn("extractor unavailable", job["error"])
        self.assertEqual(1, job["attempts"])
        self.assertFalse(learner.process_one())

    def test_invalid_continuation_rejects_before_model_and_partial_results_stay_unknown(self):
        client = SimpleNamespace(responses=FakeResponses([]))
        agent = create_default_agent(client, cwd=self.root,
                                     config=AgentConfig(collaborative_memory=CollaborativeMemoryConfig(mode="record")))
        self.addCleanup(agent.close)
        with self.assertRaisesRegex(ValueError, "unknown episode"):
            agent.run("continue wrong id", episode_id="q_missing")
        self.assertEqual([], client.responses.requests)

    def test_model_call_budget_and_literal_metacharacters(self):
        store, qid, _ = self.episode()
        current = store.begin("new", [])
        config = CollaborativeMemoryConfig(mode="learn", model_calls=2, search_rounds=1)
        client = SimpleNamespace(responses=FakeResponses([
            response([function_call("one", "collaborative_grep", '{"terms":["retry"]}')]),
            response([function_call("two", "collaborative_grep", '{"terms":["retry"]}')]),
        ]))
        material, trace = Retriever(store, config, client, "fake").retrieve(current, "retry", "implementer", [])
        self.assertEqual("", material)
        self.assertEqual(2, trace.model_calls)
        self.assertEqual(1, len(trace.searches))
        search = CorpusSearch(store, config, time.monotonic() + 10, RetrievalTrace(), current)
        self.assertEqual([], search.grep(["(a+)+$"])["hits"])

    def test_late_event_invalidates_result_and_blocks_stale_extraction(self):
        store, qid, _ = self.episode()
        store.feedback(qid, criterion="acceptance", passed=True, summary="checked")
        version = store.episode(qid)["version"]
        store.event(qid, "late-message", "message_delivered", "A missed condition", agent_id="reviewer")
        self.assertEqual("unknown", store.episode(qid)["outcome"])
        self.assertFalse(store.sources(qid)["evidence"][0]["active"])
        with self.assertRaises(ValueError):
            store.learn(qid, version, [])

    def test_blob_hash_and_path_traversal_are_rejected(self):
        store, qid, _ = self.episode()
        eid = store.event(qid, "large", "tool_result", "a" * 4000)
        event = store.rows("SELECT * FROM events WHERE id=?", (eid,))[0]
        store.path(event["blob"]).write_text("corrupted", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "corrupt"):
            store.event_body(event)
        with self.assertRaises(ValueError):
            store.path("../../outside")

    def test_transient_injection_does_not_persist_and_deleted_version_is_dropped(self):
        store, qid, event = self.episode()
        store.feedback(qid, criterion="acceptance", passed=True, summary="checked")
        verification = store.sources(qid)["evidence"][0]["event_id"]
        iid = store.learn(qid, store.episode(qid)["version"], [{
            "text": "Bound retry attempts.", "conditions": "retry", "roles": ["*"],
            "event_ids": [event, verification],
        }])[0]
        service = CollaborativeMemory(self.root, CollaborativeMemoryConfig(mode="learn"))
        self.addCleanup(service.close)
        observer = service.observer("implementer")
        client = SimpleNamespace(responses=FakeResponses([response([], "done")]))
        agent = AgentLoop(client, tool_registry=ToolRegistry(), run_observer=observer)
        material = "COLLABORATIVE_MEMORY untrusted historical lesson"
        with patch.object(Retriever, "retrieve", return_value=(material, RetrievalTrace())):
            agent.run("current task")
        self.assertIn(material, encode(client.responses.requests[0]["input"]))
        self.assertNotIn(material, encode(agent.snapshot_history()))
        observer._insight_versions = {iid: 1}
        self.assertTrue(observer.context_is_current())
        store.change_insight(iid, "disable", reason="counterexample")
        self.assertFalse(observer.context_is_current())


if __name__ == "__main__":
    unittest.main()
