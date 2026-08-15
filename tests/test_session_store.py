from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from myagent.session_store import (
    SessionMetadata,
    SessionStore,
    SessionStoreError,
    default_session_store_root,
)
from myagent.session_timeline import SessionTimeline


class BadDump:
    def model_dump(self, *, mode: str):
        raise RuntimeError("nope")


def metadata(root: Path, title: str = "会话") -> SessionMetadata:
    return SessionMetadata(title, str(root.resolve()), "main", "sub", "main")


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = SessionStore(self.base / "conversations", auto_migrate=False)
        self.store.initialize()

    def test_default_path_environment_precedence(self) -> None:
        with patch.dict(os.environ, {
            "MYAGENT_HOME": str(self.base / "new"),
            "MYAGENT_GUI_HOME": str(self.base / "gui"),
            "APPDATA": str(self.base / "appdata"),
        }, clear=True):
            self.assertEqual(default_session_store_root(), (self.base / "new" / "conversations").resolve())
        with patch.dict(os.environ, {
            "MYAGENT_GUI_HOME": str(self.base / "gui"),
            "APPDATA": str(self.base / "appdata"),
        }, clear=True):
            self.assertEqual(default_session_store_root(), (self.base / "gui" / "conversations").resolve())
        with patch.dict(os.environ, {"APPDATA": str(self.base / "appdata")}, clear=True):
            self.assertEqual(default_session_store_root(), (self.base / "appdata" / "MyAgent" / "conversations").resolve())

    def test_two_turns_append_only_and_lossless_replay(self) -> None:
        created = self.store.create_session(metadata(self.base))
        source = SessionTimeline()
        source.record_user_input([{"role": "user", "content": "first-secret-text"}])
        source.record_response_output([{"type": "reasoning", "id": "r1", "summary": []}, {"role": "assistant", "content": "one"}])
        source.record_request_succeeded()
        result = self.store.append_turn(
            created.id,
            [{"speaker": "你", "text": "first-secret-text"}],
            expected_revision=0,
            timeline=source,
        )
        self.assertEqual(result.through_operation_id, source.pending_operations[-1].operation_id)
        source.acknowledge_operations(result.through_operation_id)
        self.assertEqual(source.pending_operations, ())
        revision = result.new_revision
        size_after_first = (self.store.root / "1.jsonl").stat().st_size

        source.record_user_input([{"role": "user", "content": "second-text"}])
        source.record_response_output([{"role": "assistant", "content": "two"}])
        source.record_request_succeeded()
        result = self.store.append_turn(
            created.id,
            [{"speaker": "你", "text": "second-text"}],
            expected_revision=revision,
            timeline=source,
        )
        revision = result.new_revision
        lines = (self.store.root / "1.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertNotIn("first-secret-text", lines[2])
        self.assertGreater((self.store.root / "1.jsonl").stat().st_size, size_after_first)
        restored = self.store.load_session(created.id)
        self.assertEqual(restored.revision, revision)
        self.assertEqual(restored.timeline.operations, source.operations)
        self.assertEqual(restored.timeline.build_model_input(), source.build_model_input())
        self.assertEqual(restored.timeline.pending_operations, ())

    def test_tool_protocol_round_trip(self) -> None:
        created = self.store.create_session(metadata(self.base))
        timeline = SessionTimeline()
        timeline.record_user_input([{"role": "user", "content": "run"}])
        timeline.record_response_output([
            {"type": "reasoning", "id": "r", "summary": []},
            {"type": "function_call", "call_id": "call-1", "name": "read", "arguments": "{}"},
        ])
        timeline.record_tool_outputs([
            {"type": "function_call_output", "call_id": "call-1", "output": "ok"}
        ])
        timeline.record_request_succeeded()
        self.store.append_turn(created.id, [], expected_revision=0, timeline=timeline)
        restored = self.store.load_session(created.id)
        model_input = restored.timeline.build_model_input()
        self.assertEqual(model_input[1]["type"], "reasoning")
        self.assertEqual(model_input[2]["call_id"], model_input[3]["call_id"])

    def test_metadata_catalog_activation_stale_and_detached_results(self) -> None:
        first = self.store.create_session(metadata(self.base, "first"))
        second = self.store.create_session(metadata(self.base, "second"))
        state = self.store.activate_session(first.id)
        self.assertEqual(state.active_session_id, first.id)
        revision = self.store.append_metadata(
            first.id, metadata(self.base, "changed"), expected_revision=0
        )
        before = (self.store.root / "1.jsonl").read_bytes()
        contender = SessionStore(self.store.root, auto_migrate=False)
        with self.assertRaisesRegex(SessionStoreError, "stale"):
            contender.append_metadata(
                first.id, metadata(self.base, "stale"), expected_revision=0
            )
        self.assertEqual((self.store.root / "1.jsonl").read_bytes(), before)
        self.assertEqual(self.store.load_session(first.id).revision, revision)
        loaded = self.store.load_session(second.id)
        detached = list(loaded.messages)
        detached.append({"speaker": "x", "text": "mutated"})
        self.assertEqual(self.store.load_session(second.id).messages, ())

    def test_candidate_serialization_failures_do_not_change_file_or_pending(self) -> None:
        created = self.store.create_session(metadata(self.base))
        timeline = SessionTimeline()
        timeline.record_user_input([{"role": "user", "content": BadDump()}])
        before = (self.store.root / "1.jsonl").read_bytes()
        with self.assertRaisesRegex(SessionStoreError, "model_dump failed"):
            self.store.append_turn(created.id, [], expected_revision=0, timeline=timeline)
        self.assertEqual((self.store.root / "1.jsonl").read_bytes(), before)
        self.assertTrue(timeline.pending_operations)

        nan_timeline = SessionTimeline()
        nan_timeline.record_user_input([{"role": "user", "content": float("nan")}])
        with self.assertRaisesRegex(SessionStoreError, "non-finite"):
            self.store.append_turn(created.id, [], expected_revision=0, timeline=nan_timeline)
        self.assertEqual((self.store.root / "1.jsonl").read_bytes(), before)

    def test_secret_and_append_failure_leave_candidate_uncommitted(self) -> None:
        created = self.store.create_session(metadata(self.base))
        timeline = SessionTimeline()
        timeline.record_user_input([{"role": "user", "content": "api-secret"}])
        before = (self.store.root / "1.jsonl").read_bytes()
        with self.assertRaisesRegex(SessionStoreError, "sensitive"):
            self.store.append_turn(
                created.id, [], expected_revision=0, timeline=timeline,
                sensitive_values=("api-secret",),
            )
        self.assertEqual((self.store.root / "1.jsonl").read_bytes(), before)
        with patch.object(SessionStore, "_append_bytes", side_effect=SessionStoreError("disk")):
            with self.assertRaisesRegex(SessionStoreError, "disk"):
                self.store.append_turn(created.id, [], expected_revision=0, timeline=timeline)
        self.assertEqual((self.store.root / "1.jsonl").read_bytes(), before)
        self.assertTrue(timeline.pending_operations)

    def test_fsync_failure_rolls_back_and_same_revision_can_retry(self) -> None:
        created = self.store.create_session(metadata(self.base))
        timeline = SessionTimeline()
        timeline.record_user_input([{"role": "user", "content": "retry"}])
        path = self.store.root / f"{created.id}.jsonl"
        before = path.read_bytes()
        pending = timeline.pending_operations

        with patch("myagent.session_store.os.fsync", side_effect=[OSError("fsync"), None]):
            with self.assertRaisesRegex(SessionStoreError, "fsync"):
                self.store.append_turn(
                    created.id, [], expected_revision=0, timeline=timeline
                )

        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(timeline.pending_operations, pending)
        self.assertEqual(self.store.load_session(created.id).revision, 0)
        result = self.store.append_turn(
            created.id, [], expected_revision=0, timeline=timeline
        )
        self.assertEqual(result.new_revision, 1)

    def test_corruption_is_rejected_without_repairing_original(self) -> None:
        created = self.store.create_session(metadata(self.base))
        path = self.store.root / f"{created.id}.jsonl"
        original = path.read_bytes()
        corruptions = []
        records = [json.loads(line) for line in original.decode().splitlines()]
        unknown = deepcopy(records); unknown.append({
            "version": 1, "event": "unknown", "timestamp": "2026-01-01T00:00:00Z",
            "revision": 1, "session_id": created.id,
        }); corruptions.append((unknown, "unknown event"))
        bad_fields = deepcopy(records); bad_fields[0]["extra"] = 1; corruptions.append((bad_fields, "fields"))
        bad_json = b"{broken\n"
        for candidate, expected in corruptions:
            path.write_text("\n".join(json.dumps(item) for item in candidate) + "\n", encoding="utf-8")
            before = path.read_bytes()
            with self.assertRaisesRegex(SessionStoreError, expected):
                self.store.load_session(created.id)
            self.assertEqual(path.read_bytes(), before)
        path.write_bytes(bad_json)
        with self.assertRaisesRegex(SessionStoreError, "invalid JSON"):
            self.store.load_session(created.id)
        self.assertEqual(path.read_bytes(), bad_json)
        for constant in ("NaN", "Infinity", "-Infinity"):
            candidate = ('{"value":' + constant + '}\n').encode()
            path.write_bytes(candidate)
            with self.assertRaisesRegex(SessionStoreError, "non-standard numeric constant"):
                self.store.load_session(created.id)
            self.assertEqual(path.read_bytes(), candidate)

    def test_delete_is_physical_and_failed_delete_does_not_tombstone(self) -> None:
        created = self.store.create_session(metadata(self.base))
        catalog_before = self.store.catalog_path.read_bytes()
        with patch("myagent.session_store.Path.unlink", side_effect=OSError("locked")):
            with self.assertRaisesRegex(SessionStoreError, "unable to delete"):
                self.store.delete_session(created.id, expected_revision=0)
        self.assertEqual(self.store.catalog_path.read_bytes(), catalog_before)
        state = self.store.delete_session(created.id, expected_revision=0)
        self.assertFalse((self.store.root / "1.jsonl").exists())
        self.assertNotIn(created.id, {session.id for session in state.sessions})
        self.assertIn('"event":"session_deleted"', self.store.catalog_path.read_text(encoding="utf-8"))
        with self.assertRaises(SessionStoreError):
            self.store.activate_session(created.id)

    def test_legacy_migration_preserves_file_and_is_idempotent(self) -> None:
        legacy_path = self.base / "conversations.json"
        legacy = {
            "version": 1,
            "activeSessionId": 7,
            "nextSessionId": 10,
            "sessions": [
                {
                    "id": session_id,
                    "title": f"legacy-{session_id}",
                    "workspace": str(self.base.resolve()),
                    "mainModel": "main",
                    "subModel": "sub",
                    "kind": "main",
                    "messages": [{"speaker": "你", "text": f"ui-{session_id}"}],
                    "history": [{"role": "user", "content": f"history-{session_id}"}],
                }
                for session_id in (3, 7)
            ],
        }
        legacy_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        before = legacy_path.read_bytes()
        store = SessionStore(self.base / "migrated", legacy_path=legacy_path)
        state = store.initialize()
        self.assertEqual({item.id for item in state.sessions}, {3, 7})
        self.assertEqual(state.active_session_id, 7)
        self.assertEqual(state.next_session_id, 10)
        self.assertTrue(state.legacy_migrated)
        self.assertEqual(store.load_session(3).messages[0]["text"], "ui-3")
        self.assertEqual(store.load_session(7).timeline.build_model_input()[0]["content"], "history-7")
        catalog_before = store.catalog_path.read_bytes()
        self.assertFalse(store.migrate_legacy())
        self.assertEqual(store.catalog_path.read_bytes(), catalog_before)
        self.assertEqual(legacy_path.read_bytes(), before)

    def test_legacy_migration_rejects_runtime_secret_before_creating_jsonl(self) -> None:
        legacy_path = self.base / "secret-conversations.json"
        secret = "registered-api-key"
        payload = {
            "version": 1,
            "activeSessionId": 1,
            "nextSessionId": 2,
            "sessions": [{
                "id": 1,
                "title": "legacy",
                "workspace": str(self.base.resolve()),
                "mainModel": "main",
                "subModel": "",
                "kind": "main",
                "messages": [{"speaker": "你", "text": secret}],
                "history": [{"role": "user", "content": secret}],
            }],
        }
        legacy_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        before = legacy_path.read_bytes()
        root = self.base / "secret-migrated"
        store = SessionStore(root, legacy_path=legacy_path)

        with self.assertRaisesRegex(SessionStoreError, "sensitive value"):
            store.initialize(sensitive_values=(secret,))

        self.assertEqual(legacy_path.read_bytes(), before)
        self.assertEqual(list(root.glob("*.jsonl")), [])
        with self.assertRaisesRegex(SessionStoreError, "sensitive value"):
            store.create_session(metadata(self.base), sensitive_values=(secret,))
        self.assertEqual(legacy_path.read_bytes(), before)
        self.assertEqual(list(root.glob("*.jsonl")), [])

    def test_catalog_header_scans_constructor_and_initialize_secrets(self) -> None:
        root = self.base / "header-secret"
        api_key = "catalog-api-key"
        base_url = "https://catalog-provider.example/v1"
        store = SessionStore(
            root,
            auto_migrate=False,
            sensitive_values=(api_key,),
        )
        original_records_bytes = store._records_bytes
        observed: list[tuple[str, ...]] = []

        def recording_records_bytes(records, secrets):
            observed.append(tuple(secrets))
            return original_records_bytes(records, secrets)

        with patch.object(
            store,
            "_catalog_header",
            return_value={"event": "catalog_header", "leak": api_key},
        ), patch.object(
            store,
            "_records_bytes",
            side_effect=recording_records_bytes,
        ):
            with self.assertRaisesRegex(SessionStoreError, "sensitive value"):
                store.initialize(sensitive_values=(base_url,))

        self.assertIn(api_key, observed[0])
        self.assertIn(base_url, observed[0])
        self.assertFalse(store.catalog_path.exists())


if __name__ == "__main__":
    unittest.main()
