from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from myagent.gui_conversations import ConversationStore, ConversationStoreError


class DumpableItem:
    def model_dump(self, *, mode: str):
        if mode != "json":
            raise AssertionError(mode)
        return {"type": "reasoning", "id": "reason-1", "summary": []}


def payload(workspace: Path) -> dict[str, object]:
    return {
        "version": 1,
        "activeSessionId": 7,
        "nextSessionId": 8,
        "sessions": [{
            "id": 7,
            "title": "恢复测试",
            "workspace": str(workspace.resolve()),
            "mainModel": "main-model",
            "subModel": "sub-model",
            "kind": "main",
            "messages": [
                {"speaker": "你", "text": "检查文件"},
                {"speaker": "MyAgent", "text": "完成"},
            ],
            "history": [
                {"role": "user", "content": "检查文件"},
                DumpableItem(),
                {"type": "function_call", "call_id": "call-1", "name": "read"},
                {"type": "function_call_output", "call_id": "call-1", "output": "ok"},
            ],
        }],
    }


class ConversationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ConversationStore(self.root / "config" / "conversations.json")

    def test_missing_file_and_complete_json_round_trip(self) -> None:
        self.assertIsNone(self.store.load())
        self.store.save(payload(self.root), sensitive_values=("registered-secret",))
        restored = self.store.load()
        self.assertEqual(restored["sessions"][0]["history"][1]["type"], "reasoning")
        history = restored["sessions"][0]["history"]
        self.assertEqual(history[2]["call_id"], history[3]["call_id"])
        serialized = self.store.path.read_text(encoding="utf-8")
        self.assertNotIn("registered-secret", serialized)

    def test_invalid_roots_and_session_fields_are_rejected(self) -> None:
        cases = []
        wrong_version = payload(self.root); wrong_version["version"] = 2; cases.append(wrong_version)
        duplicate = payload(self.root); duplicate["sessions"].append(dict(duplicate["sessions"][0])); cases.append(duplicate)
        bad_active = payload(self.root); bad_active["activeSessionId"] = 99; cases.append(bad_active)
        bad_workspace = payload(self.root); bad_workspace["sessions"][0]["workspace"] = "relative"; cases.append(bad_workspace)
        bad_messages = payload(self.root); bad_messages["sessions"][0]["messages"] = [{}]; cases.append(bad_messages)
        bad_history = payload(self.root); bad_history["sessions"][0]["history"] = object(); cases.append(bad_history)
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises(ConversationStoreError):
                ConversationStore.validate(candidate)

    def test_corrupt_file_is_retained_and_replace_failure_preserves_old_json(self) -> None:
        self.store.path.parent.mkdir(parents=True)
        self.store.path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ConversationStoreError, "JSON 已损坏"):
            self.store.load()
        self.assertEqual(self.store.path.read_text(encoding="utf-8"), "{broken")

        original = json.dumps(ConversationStore.validate(payload(self.root)), ensure_ascii=False)
        self.store.path.write_text(original, encoding="utf-8")
        changed = payload(self.root)
        changed["sessions"][0]["title"] = "不应替换"
        with patch("myagent.gui_conversations.os.replace", side_effect=OSError("locked")):
            with self.assertRaisesRegex(ConversationStoreError, "无法保存"):
                self.store.save(changed)
        self.assertEqual(self.store.path.read_text(encoding="utf-8"), original)

    def test_registered_secret_in_candidate_is_refused(self) -> None:
        candidate = payload(self.root)
        candidate["sessions"][0]["history"][0]["content"] = "secret-key"
        with self.assertRaisesRegex(ConversationStoreError, "模型凭据"):
            self.store.save(candidate, sensitive_values=("secret-key",))
        self.assertFalse(self.store.path.exists())


if __name__ == "__main__":
    unittest.main()
