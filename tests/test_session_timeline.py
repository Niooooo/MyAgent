import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace

from myagent.agent import AgentLoop
from myagent.memory import (
    HISTORY_SUMMARY_PREFIX,
    ContextMemory,
    MemoryConfig,
    ToolResultStore,
)
from myagent.session_timeline import SessionTimeline, TimelineBlockView, TimelineSnapshot
from myagent.tooling import ToolRegistry
from tests.fakes import FakeResponses, function_call, response


def _config() -> MemoryConfig:
    return MemoryConfig(
        tool_summary_threshold_bytes=40,
        eager_persist_threshold_bytes=80,
        context_compaction_threshold_bytes=160,
        preview_chars=80,
        history_summary_chars=120,
        load_memory_max_chars=50,
        recent_user_turns=1,
    )


class SessionTimelineTests(unittest.TestCase):
    def test_snapshot_and_operation_views_are_isolated(self) -> None:
        timeline = SessionTimeline()
        original = {"role": "user", "content": "hello"}
        timeline.record_user_input([original])

        original["content"] = "changed outside"
        snapshot = timeline.snapshot_history()
        snapshot[0]["content"] = "changed snapshot"
        operations = timeline.operations
        operations[0].items[0]["content"] = "changed operation view"

        self.assertEqual(timeline.build_model_input()[0]["content"], "hello")

    def test_reasoning_calls_and_outputs_keep_order_and_call_ids(self) -> None:
        timeline = SessionTimeline()
        reasoning = SimpleNamespace(type="reasoning", detail="opaque")
        calls = [
            function_call("call_a", "one", "{}"),
            function_call("call_b", "two", "{}"),
        ]
        outputs = [
            {"type": "function_call_output", "call_id": "call_a", "output": "a"},
            {"type": "function_call_output", "call_id": "call_b", "output": "b"},
        ]
        timeline.record_user_input([{"role": "user", "content": "run"}])
        timeline.record_response_output([reasoning, *calls])
        timeline.record_tool_outputs(outputs)

        projected = timeline.build_model_input()
        self.assertEqual(projected[1:4], [reasoning, *calls])
        self.assertEqual(
            [item["call_id"] for item in projected[4:]],
            ["call_a", "call_b"],
        )
        self.assertTrue(timeline.active_blocks()[-1].protocol_complete)

    def test_restore_rebuilds_blocks_and_ordinary_compaction_is_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            timeline = SessionTimeline()
            call = function_call("old_call", "tool", "{}")
            old = {
                "type": "function_call_output",
                "call_id": "old_call",
                "output": "x" * 400,
            }
            timeline.restore_history(
                [
                    {"role": "user", "content": "first"},
                    call,
                    old,
                    {"role": "user", "content": "current"},
                ]
            )
            memory = ContextMemory(ToolResultStore(root), _config())
            before_operations = timeline.operations

            memory.prepare_request(timeline, lambda _source, _limit: "old facts")

            model_input = timeline.build_model_input()
            self.assertNotIn(old, model_input)
            self.assertTrue(any("old facts" in item.get("content", "") for item in model_input))
            self.assertEqual(before_operations[0].kind, "restore")
            self.assertIn(old, before_operations[0].items)
            self.assertEqual(timeline.operations[-1].kind, "compaction")

    def test_restore_keeps_developer_context_with_its_user_turn(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source = SessionTimeline()
            developer = {"role": "developer", "content": "d" * 240}
            first_user = {"role": "user", "content": "first"}
            old = {"role": "assistant", "content": "x" * 400}
            current = {"role": "user", "content": "current"}
            source.record_user_input([developer, first_user])
            source.record_response_output([old])
            source.record_request_succeeded()
            source.record_user_input([current])

            restored = SessionTimeline()
            restored.restore_history(source.snapshot_history())
            memory = ContextMemory(ToolResultStore(root), _config())
            memory.prepare_request(restored, lambda _source, _limit: "old facts")

            projected = restored.build_model_input()
            self.assertEqual(projected[:2], [developer, first_user])
            first_block = restored.active_blocks()[0]
            self.assertEqual(first_block.kind, "user")
            self.assertEqual(first_block.turn_id, 1)
            self.assertEqual(first_block.items, (developer, first_user))

    def test_restore_keeps_assistant_context_with_its_user_turn(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source = SessionTimeline()
            context = {"role": "assistant", "content": "injected " + "d" * 240}
            first_user = {"role": "user", "content": "first"}
            call = function_call("old_call", "tool", "{}")
            output = {
                "type": "function_call_output",
                "call_id": "old_call",
                "output": "x" * 400,
            }
            current = {"role": "user", "content": "current"}
            source.record_user_input([context, first_user])
            source.record_response_output([call])
            source.record_tool_outputs([output])
            source.record_request_succeeded()
            source.record_user_input([current])

            restored = SessionTimeline()
            restored.restore_history(source.snapshot_history())
            ContextMemory(ToolResultStore(root), _config()).prepare_request(
                restored,
                lambda _source, _limit: "old tool facts",
            )

            self.assertEqual(restored.build_model_input()[:2], [context, first_user])
            first_block = restored.active_blocks()[0]
            self.assertEqual(first_block.kind, "user")
            self.assertEqual(first_block.items, (context, first_user))
            self.assertEqual(restored.operations[-1].kind, "compaction")

    def test_user_prefixes_cannot_change_restore_classification(self) -> None:
        prefixes = [
            HISTORY_SUMMARY_PREFIX,
            "BACKGROUND_TOOL_RESULTS",
        ]
        for prefix in prefixes:
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as root:
                source = SessionTimeline()
                first_user = {
                    "role": "user",
                    "content": prefix + "\nuser controlled " + "u" * 240,
                }
                call = function_call("old_call", "tool", "{}")
                output = {
                    "type": "function_call_output",
                    "call_id": "old_call",
                    "output": "x" * 400,
                }
                current = {"role": "user", "content": "current"}
                source.record_user_input([first_user])
                source.record_response_output([call])
                source.record_tool_outputs([output])
                source.record_request_succeeded()
                source.record_user_input([current])

                restored = SessionTimeline()
                restored.restore_history(source.snapshot_history())
                ContextMemory(ToolResultStore(root), _config()).prepare_request(
                    restored,
                    lambda _source, _limit: "old tool facts",
                )

                self.assertEqual(restored.build_model_input()[0], first_user)
                first_block = restored.active_blocks()[0]
                self.assertEqual(first_block.kind, "user")
                self.assertEqual(first_block.turn_id, 1)
                self.assertEqual(first_block.items, (first_user,))

    def test_restore_rejects_output_before_call_transactionally(self) -> None:
        timeline = SessionTimeline()
        timeline.record_user_input([{"role": "user", "content": "existing"}])
        before_input = timeline.build_model_input()
        before_operations = timeline.operations

        malformed = [
            {"type": "function_call_output", "call_id": "c1", "output": "bad"},
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "tool",
                "arguments": "{}",
            },
        ]
        with self.assertRaisesRegex(ValueError, "invalid tool-call exchange"):
            timeline.restore_history(malformed)

        self.assertEqual(timeline.build_model_input(), before_input)
        self.assertEqual(timeline.operations, before_operations)

    def test_compaction_failures_roll_back_and_emergency_commit_is_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            timeline = SessionTimeline()
            timeline.record_user_input([{"role": "user", "content": "first"}])
            timeline.record_response_output(
                [function_call("old_call", "tool", "{}")]
            )
            timeline.record_tool_outputs(
                [{"type": "function_call_output", "call_id": "old_call", "output": "x" * 400}]
            )
            timeline.record_request_succeeded()
            timeline.record_user_input([{"role": "user", "content": "current"}])
            memory = ContextMemory(ToolResultStore(root), _config())
            before = timeline.build_model_input()
            operation_count = len(timeline.operations)
            summarizer_calls: list[str] = []

            memory.prepare_request(
                timeline,
                lambda source, _limit: summarizer_calls.append(source) or "   ",
            )
            self.assertEqual(timeline.build_model_input(), before)
            self.assertEqual(len(timeline.operations), operation_count)

            memory.prepare_request(
                timeline,
                lambda source, _limit: summarizer_calls.append(source)
                or (_ for _ in ()).throw(RuntimeError("normal summary failed")),
            )
            self.assertEqual(len(summarizer_calls), 2)
            self.assertEqual(timeline.build_model_input(), before)
            self.assertEqual(len(timeline.operations), operation_count)

            def fail(_source: str, _limit: int) -> str:
                raise RuntimeError("summary failed")

            with self.assertRaisesRegex(RuntimeError, "summary failed"):
                memory.emergency_compact(timeline, fail)
            self.assertEqual(timeline.build_model_input(), before)
            self.assertEqual(len(timeline.operations), operation_count)

            memory.emergency_compact(timeline, lambda _source, _limit: "whole history")
            self.assertEqual(len(timeline.build_model_input()), 1)
            self.assertEqual(timeline.operations[-1].kind, "emergency_compaction")
            self.assertIn("whole history", timeline.build_model_input()[0]["content"])

    def test_structured_snapshot_preserves_response_provenance_and_compacts(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source = SessionTimeline()
            reasoning = {"type": "reasoning", "detail": "opaque"}
            assistant = {"role": "assistant", "content": "a" * 400}
            source.record_user_input([{"role": "user", "content": "first"}])
            source.record_request_succeeded()
            source.record_response_output([reasoning, assistant])
            source.record_request_succeeded()
            source.record_user_input([{"role": "user", "content": "current"}])
            expected = source.active_blocks()

            restored = SessionTimeline()
            restored.restore(source.snapshot())
            self.assertEqual(restored.active_blocks(), expected)
            summary_calls: list[str] = []
            ContextMemory(ToolResultStore(root), _config()).prepare_request(
                restored,
                lambda text, _limit: summary_calls.append(text) or "old response",
            )

            self.assertEqual(len(summary_calls), 1)
            self.assertNotIn(assistant, restored.build_model_input())
            self.assertEqual(restored.operations[-1].kind, "compaction")

    def test_structured_snapshot_distinguishes_runtime_from_prefixed_user(self) -> None:
        source = SessionTimeline()
        user = {
            "role": "user",
            "content": "BACKGROUND_TOOL_RESULTS\nthis is real user text",
        }
        runtime = {
            "role": "user",
            "content": "BACKGROUND_TOOL_RESULTS\nactual runtime payload",
        }
        source.record_user_input([user])
        source.record_runtime_items([runtime])

        restored = SessionTimeline()
        restored.restore(source.snapshot())

        self.assertEqual([block.kind for block in restored.active_blocks()], ["user", "runtime"])
        self.assertEqual(restored.build_model_input(), [user, runtime])

    def test_structured_restore_rejects_malicious_snapshots_transactionally(self) -> None:
        source = SessionTimeline()
        source.record_user_input([{"role": "user", "content": "source"}])
        snapshot = source.snapshot()
        target = SessionTimeline()
        target.record_user_input([{"role": "user", "content": "existing"}])
        before_input = target.build_model_input()
        before_operations = target.operations
        malicious = [
            replace(snapshot, pending_from=True),
            replace(
                snapshot,
                operations=(replace(snapshot.operations[0], kind="unknown"),),
            ),
            replace(
                snapshot,
                operations=(replace(snapshot.operations[0], operation_id=2),),
            ),
            replace(
                snapshot,
                active_blocks=(replace(snapshot.active_blocks[0], sent="yes"),),
            ),
            replace(
                snapshot,
                active_blocks=(
                    *snapshot.active_blocks,
                    TimelineBlockView(99, "runtime", ({"role": "user", "content": "fake"},), 1, False, True),
                ),
            ),
        ]
        for bad_snapshot in malicious:
            with self.subTest(snapshot=bad_snapshot), self.assertRaises((TypeError, ValueError)):
                target.restore(bad_snapshot)  # type: ignore[arg-type]
            self.assertEqual(target.build_model_input(), before_input)
            self.assertEqual(target.operations, before_operations)

    def test_public_record_apis_reject_invalid_call_pairings_transactionally(self) -> None:
        timeline = SessionTimeline()
        timeline.record_user_input([{"role": "user", "content": "run"}])

        invalid_responses = [
            [{"type": "function_call_output", "call_id": "c1", "output": "early"}],
            [function_call("", "tool", "{}")],
            [function_call("c1", "tool", "{}"), function_call("c1", "tool", "{}")],
        ]
        for items in invalid_responses:
            before_input = timeline.build_model_input()
            before_operations = timeline.operations
            with self.assertRaises(ValueError):
                timeline.record_response_output(items)
            self.assertEqual(timeline.build_model_input(), before_input)
            self.assertEqual(timeline.operations, before_operations)

    def test_structured_restore_preserves_future_state_after_projection_replacement(self) -> None:
        for mode in ("reset", "emergency"):
            with self.subTest(mode=mode):
                source = SessionTimeline()
                source.record_user_input([{"role": "user", "content": "first"}])
                source.record_response_output([{"role": "assistant", "content": "old"}])
                if mode == "reset":
                    source.reset()
                else:
                    source.emergency_compact("whole history")

                restored = SessionTimeline()
                restored.restore(source.snapshot())
                next_user = {"role": "user", "content": "next"}
                source.record_user_input([next_user])
                restored.record_user_input([next_user])

                self.assertEqual(restored.snapshot(), source.snapshot())
                round_trip = SessionTimeline()
                round_trip.restore(restored.snapshot())
                self.assertEqual(round_trip.snapshot(), restored.snapshot())

    def test_open_exchange_rejects_interleaving_and_restores_as_open(self) -> None:
        timeline = SessionTimeline()
        timeline.record_user_input([{"role": "user", "content": "run"}])
        call = function_call("c1", "tool", "{}")
        timeline.record_response_output([call])
        restored = SessionTimeline()
        restored.restore(timeline.snapshot())

        invalid_actions = [
            lambda: restored.record_user_input([{"role": "user", "content": "later"}]),
            lambda: restored.record_response_output([{"role": "assistant", "content": "later"}]),
            lambda: restored.record_runtime_items([{"role": "user", "content": "runtime"}]),
            restored.record_request_succeeded,
        ]
        for action in invalid_actions:
            before_input = restored.build_model_input()
            before_operations = restored.operations
            with self.assertRaises(ValueError):
                action()
            self.assertEqual(restored.build_model_input(), before_input)
            self.assertEqual(restored.operations, before_operations)

        output = {"type": "function_call_output", "call_id": "c1", "output": "done"}
        restored.record_tool_outputs([output])
        runtime = {"role": "user", "content": "runtime"}
        restored.record_runtime_items([runtime])
        self.assertEqual(restored.build_model_input(), [{"role": "user", "content": "run"}, call, output, runtime])

    def test_user_and_runtime_records_reject_naked_protocol_items(self) -> None:
        timeline = SessionTimeline()
        call = function_call("c1", "tool", "{}")
        output = {"type": "function_call_output", "call_id": "c1", "output": "bad"}
        for record in (timeline.record_user_input, timeline.record_runtime_items):
            for item in (call, output):
                before = timeline.snapshot()
                with self.assertRaises(ValueError):
                    record([item])
                self.assertEqual(timeline.snapshot(), before)

        timeline.record_response_output(
            [function_call("c1", "one", "{}"), function_call("c2", "two", "{}")]
        )
        invalid_outputs = [
            [],
            [{"type": "function_call_output", "call_id": "c1", "output": "missing"}],
            [
                {"type": "function_call_output", "call_id": "c1", "output": "one"},
                {"type": "function_call_output", "call_id": "c1", "output": "duplicate"},
            ],
            [
                {"type": "function_call_output", "call_id": "c1", "output": "one"},
                {"type": "function_call_output", "call_id": "wrong", "output": "wrong"},
            ],
        ]
        for items in invalid_outputs:
            before_input = timeline.build_model_input()
            before_operations = timeline.operations
            with self.assertRaises(ValueError):
                timeline.record_tool_outputs(items)
            self.assertEqual(timeline.build_model_input(), before_input)
            self.assertEqual(timeline.operations, before_operations)

    def test_reset_empties_projection_but_keeps_operations(self) -> None:
        timeline = SessionTimeline()
        timeline.record_user_input([{"role": "user", "content": "hello"}])
        timeline.reset()

        self.assertEqual(timeline.build_model_input(), [])
        self.assertEqual([op.kind for op in timeline.operations], ["user_input", "reset"])

    def test_compaction_ids_require_exact_positive_integers(self) -> None:
        timeline = SessionTimeline()
        timeline.record_user_input([{"role": "user", "content": "first"}])
        block_id = timeline.active_blocks()[0].block_id
        for invalid_id in (True, 1.0):
            before = timeline.snapshot()
            with self.subTest(block_id=invalid_id), self.assertRaises(ValueError):
                timeline.compact_blocks([invalid_id], "summary")  # type: ignore[list-item]
            self.assertEqual(timeline.snapshot(), before)

        timeline.compact_blocks([block_id], "summary")
        restored = SessionTimeline()
        restored.restore(timeline.snapshot())
        self.assertEqual(restored.snapshot(), timeline.snapshot())

    def test_agent_history_is_snapshot_only_and_agents_are_isolated(self) -> None:
        first = AgentLoop(
            SimpleNamespace(responses=FakeResponses([response([], "one")])),
            tool_registry=ToolRegistry(),
        )
        second = AgentLoop(
            SimpleNamespace(responses=FakeResponses([response([], "two")])),
            tool_registry=ToolRegistry(),
        )

        self.assertEqual(first.run("first"), "one")
        detached = first.history
        detached.clear()
        self.assertNotEqual(first.history, [])
        self.assertEqual(second.history, [])
        self.assertIsNot(first.session_timeline, second.session_timeline)
        with self.assertRaises(AttributeError):
            first.history = []  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
