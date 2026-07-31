import json
import unittest

from myagent.hooks import HookRegistry, UserPromptSubmit
from myagent.todo import TodoList
from myagent.tools import build_default_tool_registry


class TodoToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.todo_list = TodoList()
        self.hooks = HookRegistry()
        self.registry = build_default_tool_registry(
            bash_tool=lambda command: {"ok": True, "command": command},
            hooks=self.hooks,
            todo_list=self.todo_list,
            todo_reminder_tool_calls=2,
        )

    def update(self, items: list[dict[str, str]]) -> dict[str, object]:
        return self.registry.execute(
            "update_todo_list",
            json.dumps({"items": items}),
        )

    def test_update_creates_global_list_with_three_supported_statuses(self) -> None:
        items = [
            {"content": "inspect", "status": "pending"},
            {"content": "implement", "status": "in_progress"},
            {"content": "verify", "status": "completed"},
        ]

        created = self.update(items)
        fetched = self.registry.execute("get_todo_list", "{}")

        self.assertTrue(created["ok"])
        self.assertTrue(created["changed"])
        self.assertEqual(fetched["todo_list"], items)
        self.assertFalse(fetched["all_completed"])
        self.assertFalse(fetched["verified"])

    def test_invalid_update_is_in_band_and_preserves_existing_list(self) -> None:
        original = [{"content": "implement", "status": "pending"}]
        self.update(original)

        result = self.update([{"content": "implement", "status": "blocked"}])
        fetched = self.registry.execute("get_todo_list", "{}")

        self.assertFalse(result["ok"])
        self.assertIn("must be one of", result["error"])
        self.assertEqual(fetched["todo_list"], original)

    def test_drift_reminder_is_injected_periodically_until_list_changes(self) -> None:
        self.update([{"content": "implement", "status": "pending"}])

        first = self.registry.execute("bash", '{"command":"pwd"}')
        second = self.registry.execute("bash", '{"command":"ls"}')

        self.assertNotIn("todo_reminder", first)
        self.assertEqual(second["todo_reminder"]["reason"], "todo_list_stale")
        self.assertEqual(second["todo_reminder"]["tool_calls_since_update"], 2)

        third = self.registry.execute("bash", '{"command":"pwd"}')
        fourth = self.registry.execute("bash", '{"command":"ls"}')
        self.assertNotIn("todo_reminder", third)
        self.assertEqual(fourth["todo_reminder"]["reason"], "todo_list_stale")

        self.update([{"content": "implement", "status": "in_progress"}])
        after_update = self.registry.execute("bash", '{"command":"pwd"}')
        self.assertNotIn("todo_reminder", after_update)

        unchanged = self.update(
            [{"content": "implement", "status": "in_progress"}]
        )
        self.assertFalse(unchanged["changed"])
        self.assertEqual(unchanged["todo_reminder"]["reason"], "todo_list_stale")

    def test_completed_list_requires_explicit_verification_evidence(self) -> None:
        completed = self.update(
            [{"content": "implement", "status": "completed"}]
        )

        self.assertEqual(
            completed["todo_reminder"]["reason"],
            "all_completed_but_unverified",
        )
        self.assertFalse(completed["verified"])

        verified = self.registry.execute(
            "record_todo_verification",
            json.dumps({"evidence": "python -m unittest: all tests passed"}),
        )

        self.assertTrue(verified["ok"])
        self.assertTrue(verified["verified"])
        self.assertNotIn("todo_reminder", verified)

        changed = self.update(
            [{"content": "implement and document", "status": "completed"}]
        )
        self.assertFalse(changed["verified"])
        self.assertEqual(
            changed["todo_reminder"]["reason"],
            "all_completed_but_unverified",
        )
        self.assertEqual(changed["version"], completed["version"] + 1)

    def test_unchanged_list_keeps_version_and_current_verification(self) -> None:
        items = [{"content": "verify", "status": "completed"}]
        created = self.update(items)
        verified = self.registry.execute(
            "record_todo_verification",
            '{"evidence":"tests passed"}',
        )

        unchanged = self.update(items)

        self.assertFalse(unchanged["changed"])
        self.assertEqual(unchanged["version"], created["version"])
        self.assertEqual(unchanged["version"], verified["version"])
        self.assertTrue(unchanged["verified"])

    def test_verification_is_rejected_until_all_items_are_completed(self) -> None:
        self.update([{"content": "verify", "status": "in_progress"}])

        result = self.registry.execute(
            "record_todo_verification",
            '{"evidence":"tests passed"}',
        )

        self.assertFalse(result["ok"])
        self.assertIn("before every item is completed", result["error"])

    def test_unverified_completion_is_injected_on_the_next_user_turn(self) -> None:
        self.update([{"content": "implement", "status": "completed"}])
        event = UserPromptSubmit("continue")

        self.hooks.emit(event)

        self.assertEqual(len(event.context), 1)
        self.assertEqual(event.context[0]["role"], "developer")
        self.assertIn("all_completed_but_unverified", event.context[0]["content"])


if __name__ == "__main__":
    unittest.main()
