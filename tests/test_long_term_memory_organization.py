import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from myagent.long_term_memory import (
    DELETE_MEMORY_TOOL,
    GET_MEMORY_ORGANIZATION_STATUS_TOOL,
    LONG_TERM_MEMORY_DIRECTORY,
    LONG_TERM_MEMORY_TOOL_NAMES,
    ORGANIZE_MEMORY_TOOL,
    UPDATE_MEMORY_TOOL,
    LongTermMemoryStore,
    LongTermMemoryStoreError,
    long_term_memory_tools,
)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


def _memory(index: int) -> dict[str, object]:
    return {
        "title": f"Memory {index}",
        "summary": f"Summary {index}",
        "content": f"Body {index}",
        "tags": [f"tag-{index}"],
    }


def _updated(
    memory_id: str,
    *,
    reason: str = "merge_duplicate",
) -> dict[str, object]:
    return {
        "id": memory_id,
        "title": "Canonical memory",
        "summary": "Consolidated current facts",
        "content": "Merged and conflict-resolved body",
        "tags": ["canonical", "current"],
        "reason": reason,
    }


def _deletion(
    memory_id: str,
    reason: str,
    superseded_by_id: str | None,
) -> dict[str, object]:
    return {
        "id": memory_id,
        "reason": reason,
        "superseded_by_id": superseded_by_id,
    }


class LongTermMemoryMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.workspace = Path(self.temporary_directory.name)
        self.clock = MutableClock(
            datetime(2026, 8, 3, 1, 0, tzinfo=timezone.utc)
        )
        self.store = LongTermMemoryStore(
            self.workspace,
            merge_change_threshold=3,
            merge_min_interval_seconds=60,
            clock=self.clock,
        )

    def _assert_error(self, code: str, callable_, *args, **kwargs) -> None:
        with self.assertRaises(LongTermMemoryStoreError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn(str(self.workspace), raised.exception.error)

    def test_update_preserves_identity_and_noop_does_not_count_as_mutation(
        self,
    ) -> None:
        created = self.store.store(**_memory(1))
        initial_status = self.store.organization_status()

        unchanged = self.store.update(created.id, **_memory(1))
        after_noop = self.store.organization_status()

        self.clock.advance(seconds=10)
        changed = self.store.update(
            created.id,
            title="Updated title",
            summary="Updated summary",
            content="Updated body",
            tags=["updated"],
        )
        after_update = self.store.organization_status()
        extracted = self.store.extract(created.id)

        self.assertFalse(unchanged.updated)
        self.assertEqual(unchanged.metadata, created)
        self.assertEqual(
            after_noop.pending_mutations,
            initial_status.pending_mutations,
        )
        self.assertTrue(changed.updated)
        self.assertEqual(changed.metadata.id, created.id)
        self.assertEqual(changed.metadata.created_at, created.created_at)
        self.assertNotEqual(changed.metadata.updated_at, created.updated_at)
        self.assertEqual(extracted.content, "Updated body")
        self.assertEqual(after_update.pending_mutations, 2)

    def test_delete_removes_authority_without_returning_or_revealing_body(
        self,
    ) -> None:
        created = self.store.store(**_memory(1))
        entry_path = (
            self.workspace
            / LONG_TERM_MEMORY_DIRECTORY
            / "entries"
            / f"{created.id}.json"
        )

        deleted = self.store.delete(created.id)
        status = self.store.organization_status()

        self.assertEqual(deleted, created)
        self.assertEqual(self.store.search("Memory 1"), ())
        self._assert_error("memory_not_found", self.store.extract, created.id)
        self.assertFalse(entry_path.exists())
        self.assertEqual(status.pending_mutations, 2)
        self._assert_error("memory_not_found", self.store.delete, created.id)
        self.assertEqual(self.store.organization_status(), status)

    def test_state_is_internal_validated_and_empty_status_has_no_side_effects(
        self,
    ) -> None:
        empty_workspace = self.workspace / "empty"
        empty_workspace.mkdir()
        empty = LongTermMemoryStore(empty_workspace)

        status = empty.organization_status()

        self.assertEqual(status.pending_mutations, 0)
        self.assertFalse(status.organization_due)
        self.assertFalse(
            (empty_workspace / LONG_TERM_MEMORY_DIRECTORY).exists()
        )

        self.store.store(**_memory(1))
        state_path = self.workspace / LONG_TERM_MEMORY_DIRECTORY / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        catalog = json.loads(
            (
                self.workspace
                / LONG_TERM_MEMORY_DIRECTORY
                / "catalog.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(state),
            {
                "schema_version",
                "pending_mutations",
                "pending_since",
                "last_organized_at",
            },
        )
        self.assertNotIn("pending_mutations", catalog)

        state_path.write_text("{broken", encoding="utf-8")
        self._assert_error(
            "memory_organization_state_corrupt",
            self.store.organization_status,
        )

    def test_clock_rollback_and_non_finite_interval_are_rejected(self) -> None:
        self.store.store(**_memory(1))
        self.clock.value -= timedelta(seconds=1)

        self._assert_error(
            "memory_clock_error",
            self.store.organization_status,
        )
        for invalid_interval in (float("nan"), float("inf")):
            with self.subTest(invalid_interval=invalid_interval):
                with self.assertRaises(ValueError):
                    LongTermMemoryStore(
                        self.workspace,
                        merge_min_interval_seconds=invalid_interval,
                    )

    def test_update_failure_rolls_back_catalog_entry_and_state(self) -> None:
        created = self.store.store(**_memory(1))
        root = self.workspace / LONG_TERM_MEMORY_DIRECTORY
        tracked = [
            root / "catalog.json",
            root / "state.json",
            root / "entries" / f"{created.id}.json",
        ]
        before = {path: path.read_bytes() for path in tracked}
        real_apply = self.store._apply_target_bytes
        failed = False

        def fail_state_once(target: Path, content: bytes | None) -> None:
            nonlocal failed
            if target == self.store.state_path and not failed:
                failed = True
                raise OSError("private state failure")
            real_apply(target, content)

        with mock.patch.object(
            self.store,
            "_apply_target_bytes",
            side_effect=fail_state_once,
        ):
            self._assert_error(
                "memory_storage_error",
                self.store.update,
                created.id,
                title="Changed",
                summary="Changed",
                content="Changed",
                tags=[],
            )

        self.assertEqual({path: path.read_bytes() for path in tracked}, before)
        self.assertEqual(self.store.extract(created.id).content, "Body 1")

    def test_prepared_transaction_is_recovered_before_the_next_read(self) -> None:
        created = self.store.store(**_memory(1))
        real_apply = self.store._apply_target_bytes
        failed = False

        def fail_state_once(target: Path, content: bytes | None) -> None:
            nonlocal failed
            if target == self.store.state_path and not failed:
                failed = True
                raise OSError("simulated process interruption")
            real_apply(target, content)

        with (
            mock.patch.object(
                self.store,
                "_apply_target_bytes",
                side_effect=fail_state_once,
            ),
            mock.patch.object(
                self.store,
                "_rollback_transaction",
                side_effect=OSError("rollback interrupted"),
            ),
        ):
            self._assert_error(
                "memory_storage_error",
                self.store.update,
                created.id,
                title="Interrupted",
                summary="Interrupted",
                content="Interrupted",
                tags=[],
            )

        recovered = LongTermMemoryStore(
            self.workspace,
            merge_change_threshold=3,
            merge_min_interval_seconds=60,
            clock=self.clock,
        )

        self.assertEqual(recovered.extract(created.id).content, "Body 1")
        self.assertEqual(recovered.search("Interrupted"), ())
        transaction_root = (
            self.workspace / LONG_TERM_MEMORY_DIRECTORY / ".transactions"
        )
        self.assertEqual(list(transaction_root.iterdir()), [])

    def test_committed_transaction_is_rolled_forward_before_next_read(self) -> None:
        created = self.store.store(**_memory(1))

        with mock.patch.object(
            self.store,
            "_cleanup_transaction",
            side_effect=OSError("cleanup interrupted"),
        ):
            updated = self.store.update(
                created.id,
                title="Committed",
                summary="Committed",
                content="Committed body",
                tags=[],
            )

        self.assertTrue(updated.updated)
        recovered = LongTermMemoryStore(
            self.workspace,
            merge_change_threshold=3,
            merge_min_interval_seconds=60,
            clock=self.clock,
        )

        self.assertEqual(recovered.extract(created.id).content, "Committed body")
        transaction_root = (
            self.workspace / LONG_TERM_MEMORY_DIRECTORY / ".transactions"
        )
        self.assertEqual(list(transaction_root.iterdir()), [])


class LongTermMemoryOrganizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.workspace = Path(self.temporary_directory.name)
        self.clock = MutableClock(
            datetime(2026, 8, 3, 2, 0, tzinfo=timezone.utc)
        )

    def _store(self, *, threshold: int, interval: float) -> LongTermMemoryStore:
        return LongTermMemoryStore(
            self.workspace,
            merge_change_threshold=threshold,
            merge_min_interval_seconds=interval,
            clock=self.clock,
        )

    def test_organization_requires_change_and_time_thresholds_with_exact_boundary(
        self,
    ) -> None:
        store = self._store(threshold=2, interval=60)
        canonical = store.store(**_memory(1))
        duplicate = store.store(**_memory(2))
        plan = {
            "updates": [_updated(canonical.id)],
            "deletions": [
                _deletion(duplicate.id, "duplicate", canonical.id)
            ],
        }

        before = store.organization_status()
        too_early = store.organize(plan)
        self.clock.advance(seconds=59)
        still_early = store.organize(plan)
        self.clock.advance(seconds=1)
        organized = store.organize(plan)
        after = store.organization_status()

        self.assertTrue(before.changes_ready)
        self.assertFalse(before.time_ready)
        self.assertFalse(too_early.organized)
        self.assertEqual(too_early.reason, "time_threshold_not_met")
        self.assertFalse(still_early.organized)
        self.assertTrue(organized.organized)
        self.assertEqual(organized.reason, "organized")
        self.assertEqual(
            store.extract(canonical.id).content,
            _updated(canonical.id)["content"],
        )
        self.assertEqual(store.search(duplicate.id), ())
        self.assertEqual(after.pending_mutations, 0)
        self.assertIsNone(after.pending_since)
        self.assertEqual(
            after.last_organized_at,
            self.clock.value.isoformat(timespec="microseconds").replace(
                "+00:00",
                "Z",
            ),
        )

    def test_all_deletion_reasons_apply_in_one_complete_plan(self) -> None:
        store = self._store(threshold=4, interval=0)
        canonical = store.store(**_memory(1))
        duplicate = store.store(**_memory(2))
        conflicting = store.store(**_memory(3))
        outdated = store.store(**_memory(4))
        plan = {
            "updates": [
                _updated(canonical.id, reason="resolve_conflict")
            ],
            "deletions": [
                _deletion(duplicate.id, "duplicate", canonical.id),
                _deletion(conflicting.id, "conflicting", canonical.id),
                _deletion(outdated.id, "outdated", None),
            ],
        }

        result = store.organize(plan)

        self.assertTrue(result.organized)
        self.assertEqual(
            {deletion.reason for deletion in result.deleted},
            {"duplicate", "conflicting", "outdated"},
        )
        self.assertEqual(
            {item.id for item in store.search("Memory", limit=10)},
            {canonical.id},
        )
        self.assertEqual(store.search("Canonical"), (result.updated[0],))
        for removed in (duplicate, conflicting, outdated):
            with self.assertRaises(LongTermMemoryStoreError) as raised:
                store.extract(removed.id)
            self.assertEqual(raised.exception.code, "memory_not_found")

    def test_invalid_plan_references_and_overlap_never_mutate_state(self) -> None:
        store = self._store(threshold=2, interval=0)
        canonical = store.store(**_memory(1))
        duplicate = store.store(**_memory(2))
        root = self.workspace / LONG_TERM_MEMORY_DIRECTORY
        before = {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*.json")
        }
        invalid_plans = [
            {
                "updates": [_updated(canonical.id)],
                "deletions": [
                    _deletion(canonical.id, "duplicate", duplicate.id)
                ],
            },
            {
                "updates": [],
                "deletions": [
                    _deletion(duplicate.id, "duplicate", "f" * 32)
                ],
            },
            {
                "updates": [],
                "deletions": [
                    _deletion(duplicate.id, "duplicate", None)
                ],
            },
        ]

        for plan in invalid_plans:
            with self.subTest(plan=plan):
                with self.assertRaises(LongTermMemoryStoreError) as raised:
                    store.organize(plan)
                self.assertEqual(
                    raised.exception.code,
                    "invalid_organization_plan",
                )

        after = {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*.json")
        }
        self.assertEqual(after, before)

    def test_noop_organization_does_not_reset_pending_thresholds(self) -> None:
        store = self._store(threshold=1, interval=0)
        created = store.store(**_memory(1))
        plan = {
            "updates": [
                {
                    "id": created.id,
                    **_memory(1),
                    "reason": "refresh_outdated",
                }
            ],
            "deletions": [],
        }

        result = store.organize(plan)
        status = store.organization_status()

        self.assertFalse(result.organized)
        self.assertEqual(result.reason, "no_changes")
        self.assertEqual(status.pending_mutations, 1)
        self.assertIsNotNone(status.pending_since)
        self.assertIsNone(status.last_organized_at)

    def test_below_change_threshold_skips_stale_plan_ids(self) -> None:
        store = self._store(threshold=2, interval=0)
        store.store(**_memory(1))
        stale_plan = {
            "updates": [],
            "deletions": [_deletion("f" * 32, "outdated", None)],
        }

        result = store.organize(stale_plan)

        self.assertFalse(result.organized)
        self.assertEqual(result.reason, "change_threshold_not_met")


class LongTermMemoryManagementToolTests(unittest.TestCase):
    def test_management_tools_return_bounded_results_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock(
                datetime(2026, 8, 3, 3, 0, tzinfo=timezone.utc)
            )
            store = LongTermMemoryStore(
                directory,
                merge_change_threshold=1,
                merge_min_interval_seconds=0,
                clock=clock,
            )
            tools = {tool.name: tool for tool in long_term_memory_tools(store)}
            created = tools["store_memory"].handler(**_memory(1))
            updated = tools[UPDATE_MEMORY_TOOL].handler(
                id=created["id"],
                title="Updated",
                summary="Updated",
                content="Secret updated body",
                tags=[],
            )
            status = tools[GET_MEMORY_ORGANIZATION_STATUS_TOOL].handler()
            deleted = tools[DELETE_MEMORY_TOOL].handler(id=created["id"])

            self.assertEqual(set(tools), LONG_TERM_MEMORY_TOOL_NAMES)
            self.assertTrue(updated["updated"])
            self.assertNotIn("content", updated)
            self.assertTrue(status["organization_due"])
            self.assertTrue(deleted["deleted"])
            self.assertNotIn("content", deleted)

    def test_organize_tool_does_not_echo_updated_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LongTermMemoryStore(
                directory,
                merge_change_threshold=2,
                merge_min_interval_seconds=0,
            )
            tools = {tool.name: tool for tool in long_term_memory_tools(store)}
            canonical = store.store(**_memory(1))
            duplicate = store.store(**_memory(2))
            result = tools[ORGANIZE_MEMORY_TOOL].handler(
                plan={
                    "updates": [_updated(canonical.id)],
                    "deletions": [
                        _deletion(duplicate.id, "duplicate", canonical.id)
                    ],
                }
            )

            self.assertTrue(result["organized"])
            self.assertNotIn("content", json.dumps(result))
            self.assertEqual(result["deleted"][0]["reason"], "duplicate")


if __name__ == "__main__":
    unittest.main()
