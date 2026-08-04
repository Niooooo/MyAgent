import json
import tempfile
import unittest
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from myagent.agent import AgentLoop
from myagent.composition import AgentConfig, build_default_components
from myagent.hooks import HookRegistry, PostToolUse
from myagent.memory import (
    HISTORY_SUMMARY_PREFIX,
    LOAD_MEMORY_TOOL,
    ContextMemory,
    MemoryConfig,
    ToolResultStore,
    memory_tools,
)
from myagent.tooling import FunctionTool, ToolRegistry
from tests.fakes import FakeAPIError, FakeResponses, function_call, response


def _small_config(**overrides: int) -> MemoryConfig:
    values = {
        "tool_summary_threshold_bytes": 100,
        "eager_persist_threshold_bytes": 300,
        "context_compaction_threshold_bytes": 600,
        "preview_chars": 80,
        "history_summary_chars": 400,
        "load_memory_max_chars": 50,
        "recent_user_turns": 2,
    }
    values.update(overrides)
    return MemoryConfig(**values)


def _tool(name: str, result: dict[str, object], hooks: HookRegistry | None = None) -> ToolRegistry:
    return ToolRegistry(
        [
            FunctionTool(
                name=name,
                description="Return a test result",
                parameters={"type": "object", "properties": {}},
                handler=lambda: result,
            )
        ],
        hooks=hooks,
    )


def _protocol_counts(history: list[object]) -> tuple[Counter[str], Counter[str]]:
    calls: Counter[str] = Counter()
    outputs: Counter[str] = Counter()
    for item in history:
        item_type = item.get("type") if isinstance(item, dict) else item.type
        if item_type == "function_call":
            call_id = item.get("call_id") if isinstance(item, dict) else item.call_id
            calls[call_id] += 1
        elif item_type == "function_call_output":
            outputs[item["call_id"]] += 1
    return calls, outputs


class MemoryConfigTests(unittest.TestCase):
    def test_integer_fields_reject_bool_zero_and_negative_values(self) -> None:
        defaults = asdict(MemoryConfig())
        for field_name in defaults:
            for value, exception in ((False, TypeError), (0, ValueError), (-1, ValueError)):
                with self.subTest(field=field_name, value=value):
                    selected = dict(defaults)
                    selected[field_name] = value
                    with self.assertRaises(exception):
                        MemoryConfig(**selected)

    def test_threshold_order_and_agent_nested_config_are_validated(self) -> None:
        with self.assertRaisesRegex(ValueError, "tool_summary < eager_persist"):
            MemoryConfig(eager_persist_threshold_bytes=8_192)
        with self.assertRaisesRegex(TypeError, "memory must be a MemoryConfig"):
            AgentConfig(memory=True)  # type: ignore[arg-type]


class ToolResultStoreTests(unittest.TestCase):
    def test_segmented_reads_reconstruct_exact_utf8_and_reject_bad_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = ToolResultStore(temporary_directory)
            original = '{"内容": "甲乙丙丁", "ok": true}'

            ref = store.persist(original)
            pieces: list[str] = []
            offset = 0
            while True:
                segment = store.read(
                    ref,
                    offset,
                    3,
                    max_chars_limit=3,
                )
                self.assertTrue(segment["ok"])
                pieces.append(segment["content"])
                if segment["eof"]:
                    break
                offset = segment["next_offset"]

            self.assertEqual("".join(pieces), original)
            self.assertRegex(ref, r"^memory://tool-result/[0-9a-f]{32}$")
            invalid = store.read("memory://tool-result/../../secret")
            missing = store.read("memory://tool-result/" + "0" * 32)
            bad_offset = store.read(ref, True)
            too_far = store.read(ref, len(original) + 1)
            too_large = store.read(ref, 0, 4, max_chars_limit=3)

            self.assertEqual(invalid["code"], "invalid_memory_ref")
            self.assertEqual(missing["code"], "memory_not_found")
            self.assertEqual(bad_offset["code"], "invalid_memory_offset")
            self.assertEqual(too_far["code"], "invalid_memory_offset")
            self.assertEqual(too_large["code"], "invalid_memory_max_chars")
            for failure in (invalid, missing, bad_offset, too_far, too_large):
                self.assertNotIn(temporary_directory, json.dumps(failure))
                self.assertNotIn("traceback", failure)

    def test_read_tool_has_a_bounded_default_and_no_enumeration_surface(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = ToolResultStore(temporary_directory)
            ref = store.persist("abcdefghij")
            registry = ToolRegistry(memory_tools(store, max_chars=4))

            first = registry.execute(LOAD_MEMORY_TOOL, json.dumps({"ref": ref}))
            second = registry.execute(
                LOAD_MEMORY_TOOL,
                json.dumps({"ref": ref, "offset": first["next_offset"]}),
            )
            forged = registry.execute(
                LOAD_MEMORY_TOOL,
                json.dumps({"ref": ref, "max_chars": 5}),
            )

            self.assertEqual(first["content"], "abcd")
            self.assertEqual(second["content"], "efgh")
            self.assertEqual(forged["code"], "invalid_memory_max_chars")
            self.assertFalse(hasattr(store, "list"))


class ToolResultPolicyTests(unittest.TestCase):
    def test_small_output_is_unchanged_and_does_not_create_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = ToolResultStore(temporary_directory)
            memory = ContextMemory(store, _small_config())
            result = {"ok": True, "content": "small"}
            responses = FakeResponses(
                [response([function_call(name="tiny", arguments="{}")]), response([], "done")]
            )
            agent = AgentLoop(
                SimpleNamespace(responses=responses),
                tool_registry=_tool("tiny", result),
                context_memory=memory,
            )

            self.assertEqual(agent.run("run"), "done")

            self.assertEqual(
                responses.requests[1]["input"][-1]["output"],
                json.dumps(result, ensure_ascii=False),
            )
            self.assertFalse(store.root.exists())

    def test_eager_output_is_exactly_persisted_after_post_hook_and_previewed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = ToolResultStore(temporary_directory)
            memory = ContextMemory(store, _small_config())
            result = {
                "ok": False,
                "code": "large_failure",
                "error": "bounded error",
                "content": "大" * 500,
            }
            original = json.dumps(result, ensure_ascii=False)
            observed: list[dict[str, object]] = []
            hooks = HookRegistry()
            hooks.register(PostToolUse, lambda event: observed.append(dict(event.result)))
            responses = FakeResponses(
                [response([function_call(name="large", arguments="{}")]), response([], "done")]
            )
            agent = AgentLoop(
                SimpleNamespace(responses=responses),
                tool_registry=_tool("large", result, hooks),
                hooks=hooks,
                context_memory=memory,
            )

            agent.run("run")

            delivered = json.loads(responses.requests[1]["input"][-1]["output"])
            self.assertEqual(observed, [result])
            self.assertEqual(delivered["representation"], "preview")
            self.assertFalse(delivered["ok"])
            self.assertEqual(delivered["code"], "large_failure")
            self.assertEqual(delivered["error"], "bounded error")
            self.assertEqual(delivered["original_bytes"], len(original.encode("utf-8")))
            self.assertNotIn(temporary_directory, delivered["ref"])
            stored = store.read(delivered["ref"], 0, 2_000, max_chars_limit=2_000)
            self.assertEqual(stored["content"], original)

            agent.reset()
            self.assertEqual(agent.history, [])
            self.assertEqual(memory.block_count, 0)
            self.assertEqual(
                store.read(delivered["ref"], 0, 2_000, max_chars_limit=2_000)["content"],
                original,
            )

    def test_medium_output_is_persisted_and_replaced_by_deterministic_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = ToolResultStore(temporary_directory)
            memory = ContextMemory(store, _small_config())
            result = {"ok": True, "code": "listed", "content": "x" * 150}
            original = json.dumps(result, ensure_ascii=False)
            responses = FakeResponses(
                [response([function_call(name="medium", arguments="{}")]), response([], "done")]
            )
            agent = AgentLoop(
                SimpleNamespace(responses=responses),
                tool_registry=_tool("medium", result),
                context_memory=memory,
            )

            agent.run("run")

            delivered = json.loads(responses.requests[1]["input"][-1]["output"])
            self.assertEqual(delivered["representation"], "summary")
            self.assertTrue(delivered["ok"])
            self.assertEqual(delivered["code"], "listed")
            self.assertEqual(delivered["original_bytes"], len(original.encode("utf-8")))
            self.assertEqual(delivered["content_hints"][0]["path"], "content")
            stored = store.read(delivered["ref"], 0, 500, max_chars_limit=500)
            self.assertEqual(stored["content"], original)

    def test_persistence_failure_keeps_complete_original_without_false_claim(self) -> None:
        class FailingStore(ToolResultStore):
            def persist(self, serialized_result: str) -> str:
                raise OSError("disk unavailable")

        with tempfile.TemporaryDirectory() as temporary_directory:
            store = FailingStore(temporary_directory)
            memory = ContextMemory(store, _small_config())
            result = {"ok": True, "content": "x" * 500}
            original = json.dumps(result, ensure_ascii=False)
            responses = FakeResponses(
                [response([function_call(name="large", arguments="{}")]), response([], "done")]
            )
            agent = AgentLoop(
                SimpleNamespace(responses=responses),
                tool_registry=_tool("large", result),
                context_memory=memory,
            )

            agent.run("run")

            delivered = responses.requests[1]["input"][-1]["output"]
            self.assertEqual(delivered, original)
            self.assertNotIn("memory://", delivered)

    def test_reasoning_multiple_calls_and_outputs_keep_original_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = ToolResultStore(temporary_directory)
            memory = ContextMemory(store, _small_config())
            reasoning = SimpleNamespace(type="reasoning", detail="opaque")
            calls = [
                function_call("call_a", "one", "{}"),
                function_call("call_b", "two", "{}"),
            ]
            registry = ToolRegistry(
                [
                    FunctionTool(
                        name="one",
                        description="First",
                        parameters={"type": "object", "properties": {}},
                        handler=lambda: {"ok": True, "content": "a" * 150},
                    ),
                    FunctionTool(
                        name="two",
                        description="Second",
                        parameters={"type": "object", "properties": {}},
                        handler=lambda: {
                            "ok": False,
                            "code": "two_failed",
                            "error": "no",
                            "content": "b" * 150,
                        },
                    ),
                ]
            )
            responses = FakeResponses(
                [response([reasoning, *calls]), response([], "done")]
            )
            agent = AgentLoop(
                SimpleNamespace(responses=responses),
                tool_registry=registry,
                context_memory=memory,
            )

            agent.run("run both")

            exchange = responses.requests[1]["input"][1:]
            self.assertEqual(exchange[:3], [reasoning, *calls])
            outputs = exchange[3:]
            self.assertEqual([item["call_id"] for item in outputs], ["call_a", "call_b"])
            self.assertEqual(
                [json.loads(item["output"])["representation"] for item in outputs],
                ["summary", "summary"],
            )
            calls_count, outputs_count = _protocol_counts(responses.requests[1]["input"])
            self.assertEqual(calls_count, outputs_count)
            self.assertTrue(all(count == 1 for count in calls_count.values()))


class HistoryCompactionTests(unittest.TestCase):
    def test_model_summarizes_only_complete_sent_middle_blocks_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            memory = ContextMemory(
                ToolResultStore(temporary_directory),
                _small_config(history_summary_chars=1_200),
            )
            history: list[object] = []
            users: list[dict[str, str]] = []
            current_exchange: list[object] = []

            for turn in range(1, 5):
                user = {"role": "user", "content": f"goal-{turn} " + "u" * 180}
                users.append(user)
                history.append(user)
                memory.record_user_turn([user])
                if turn < 4:
                    memory.mark_request_succeeded()

                reasoning = {
                    "type": "reasoning",
                    "detail": f"secret-chain-{turn}",
                }
                call = {
                    "type": "function_call",
                    "call_id": f"call_{turn}",
                    "name": f"tool_{turn}",
                    "arguments": "{}",
                }
                output = {
                    "type": "function_call_output",
                    "call_id": f"call_{turn}",
                    "output": json.dumps(
                        {
                            "ok": turn != 2,
                            "code": f"code_{turn}",
                            "content": "r" * 150,
                        }
                    ),
                }
                history.extend([reasoning, call])
                memory.record_response([reasoning, call])
                history.append(output)
                memory.record_tool_outputs([output])
                if turn < 4:
                    memory.mark_request_succeeded()
                else:
                    current_exchange = [reasoning, call, output]

                if turn < 4:
                    conclusion = {
                        "type": "message",
                        "role": "assistant",
                        "content": f"conclusion-{turn} " + "a" * 180,
                    }
                    history.append(conclusion)
                    memory.record_response([conclusion])
                    memory.mark_request_succeeded()

            summary_sources: list[str] = []

            def summarize(source: str, max_chars: int) -> str:
                summary_sources.append(source)
                self.assertEqual(max_chars, 1_200)
                return (
                    "User goal or constraint: goal-2. "
                    "Assistant conclusion: conclusion-1. "
                    "Tool tool_1 completed with code_1."
                )

            memory.prepare_request(history, summarize)

            summaries = [
                item
                for item in history
                if isinstance(item, dict)
                and item.get("type") == "message"
                and isinstance(item.get("content"), str)
                and item["content"].startswith(HISTORY_SUMMARY_PREFIX)
            ]
            self.assertEqual(len(summaries), 1)
            summary_text = summaries[0]["content"]
            self.assertEqual(len(summary_sources), 1)
            self.assertIn("User goal or constraint", summary_text)
            self.assertIn("Assistant conclusion", summary_text)
            self.assertIn("Tool tool_1", summary_text)
            self.assertIn("code_1", summary_text)
            self.assertIn("goal-2", summary_sources[0])
            self.assertIn("tool_1", summary_sources[0])
            self.assertIn("code_1", summary_sources[0])
            self.assertIn("memory://tool-result/", summary_sources[0])
            self.assertIn("reasoning content omitted", summary_sources[0])
            self.assertNotIn("secret-chain", summary_text)
            self.assertNotIn("secret-chain", summary_sources[0])
            self.assertIn(users[0], history)
            self.assertIn(users[2], history)
            self.assertIn(users[3], history)
            for item in current_exchange:
                self.assertTrue(any(entry is item for entry in history))

            calls, outputs = _protocol_counts(history)
            self.assertEqual(calls, outputs)
            self.assertTrue(all(count == 1 for count in calls.values()))

            memory.mark_request_succeeded()
            memory.prepare_request(history, summarize)
            self.assertEqual(memory.summary_count, 1)
            self.assertEqual(len(summary_sources), 1)
            self.assertEqual(
                sum(
                    isinstance(item, dict)
                    and item.get("type") == "message"
                    and str(item.get("content", "")).startswith(HISTORY_SUMMARY_PREFIX)
                    for item in history
                ),
                1,
            )

    def test_unsent_history_without_a_safe_middle_block_is_left_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            memory = ContextMemory(ToolResultStore(temporary_directory), _small_config())
            user = {"role": "user", "content": "current " + "x" * 800}
            call = {
                "type": "function_call",
                "call_id": "current_call",
                "name": "current_tool",
                "arguments": "{}",
            }
            output = {
                "type": "function_call_output",
                "call_id": "current_call",
                "output": json.dumps({"ok": True}),
            }
            history: list[object] = [user, call, output]
            memory.record_user_turn([user])
            memory.record_response([call])
            memory.record_tool_outputs([output])
            before = list(history)

            summarizer_calls: list[str] = []
            memory.prepare_request(
                history,
                lambda source, max_chars: summarizer_calls.append(source) or "unused",
            )

            self.assertEqual(history, before)
            self.assertEqual(memory.summary_count, 0)
            self.assertEqual(summarizer_calls, [])

    def test_model_summary_failure_or_empty_text_preserves_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            memory = ContextMemory(
                ToolResultStore(temporary_directory),
                _small_config(recent_user_turns=1),
            )
            first = {"role": "user", "content": "first task"}
            old_conclusion = {
                "type": "message",
                "role": "assistant",
                "content": "old conclusion " + "x" * 800,
            }
            current = {"role": "user", "content": "current task"}
            history: list[object] = [first, old_conclusion, current]
            memory.record_user_turn([first])
            memory.mark_request_succeeded()
            memory.record_response([old_conclusion])
            memory.mark_request_succeeded()
            memory.record_user_turn([current])
            before = list(history)

            def fail(_source: str, _max_chars: int) -> str:
                raise RuntimeError("summary request failed")

            memory.prepare_request(history, fail)
            self.assertEqual(history, before)
            self.assertEqual(memory.summary_count, 0)

            memory.prepare_request(history, lambda source, max_chars: "   ")
            self.assertEqual(history, before)
            self.assertEqual(memory.summary_count, 0)

    def test_agent_uses_a_separate_tool_free_request_for_history_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            memory = ContextMemory(
                ToolResultStore(temporary_directory),
                _small_config(recent_user_turns=1),
            )
            responses = FakeResponses(
                [
                    response([], "model-generated history summary"),
                    response([], "main answer"),
                ]
            )
            agent = AgentLoop(
                SimpleNamespace(responses=responses),
                tool_registry=_tool("noop", {"ok": True}),
                context_memory=memory,
            )
            first = {"role": "user", "content": "first task"}
            old_conclusion = {
                "type": "message",
                "role": "assistant",
                "content": "old conclusion " + "x" * 800,
            }
            agent.history.extend([first, old_conclusion])
            memory.record_user_turn([first])
            memory.mark_request_succeeded()
            memory.record_response([old_conclusion])
            memory.mark_request_succeeded()

            answer = agent.run("current task")

            self.assertEqual(answer, "main answer")
            self.assertEqual(len(responses.requests), 2)
            summary_request, main_request = responses.requests
            self.assertEqual(summary_request["tools"], [])
            self.assertIn("historical data", summary_request["instructions"])
            self.assertIn(
                "Historical conversation data",
                summary_request["input"][0]["content"],
            )
            self.assertEqual(
                [tool["name"] for tool in main_request["tools"]],
                ["noop"],
            )
            summaries = [
                item
                for item in main_request["input"]
                if isinstance(item, dict)
                and str(item.get("content", "")).startswith(HISTORY_SUMMARY_PREFIX)
            ]
            self.assertEqual(len(summaries), 1)
            self.assertIn("model-generated history summary", summaries[0]["content"])
            self.assertFalse(
                any(
                    isinstance(item, dict)
                    and item.get("content") == "model-generated history summary"
                    for item in agent.history
                )
            )

    def test_context_limit_after_normal_compaction_summarizes_complete_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            memory = ContextMemory(
                ToolResultStore(temporary_directory),
                _small_config(recent_user_turns=1),
            )
            responses = FakeResponses(
                [
                    response([], "ordinary partial-history summary"),
                    FakeAPIError(
                        400,
                        body={"error": {"code": "context_length_exceeded"}},
                    ),
                    response([], "emergency whole-history summary"),
                    response([], "main answer"),
                ]
            )
            agent = AgentLoop(
                SimpleNamespace(responses=responses),
                tool_registry=_tool("noop", {"ok": True}),
                context_memory=memory,
            )
            first = {"role": "user", "content": "first task"}
            old_conclusion = {
                "type": "message",
                "role": "assistant",
                "content": "old conclusion " + "x" * 800,
            }
            agent.history.extend([first, old_conclusion])
            memory.record_user_turn([first])
            memory.mark_request_succeeded()
            memory.record_response([old_conclusion])
            memory.mark_request_succeeded()

            self.assertEqual(agent.run("current task"), "main answer")

            self.assertEqual(len(responses.requests), 4)
            normal_summary, failed_main, emergency_summary, retried_main = (
                responses.requests
            )
            self.assertEqual(normal_summary["tools"], [])
            self.assertNotEqual(failed_main["tools"], [])
            self.assertEqual(emergency_summary["tools"], [])
            emergency_source = emergency_summary["input"][0]["content"]
            self.assertIn("first task", emergency_source)
            self.assertIn("current task", emergency_source)
            self.assertIn("ordinary partial-history summary", emergency_source)
            self.assertEqual(len(retried_main["input"]), 1)
            self.assertIn(
                "emergency whole-history summary",
                retried_main["input"][0]["content"],
            )
            self.assertEqual(memory.summary_count, 1)

    def test_emergency_summary_failure_preserves_history_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            memory = ContextMemory(
                ToolResultStore(temporary_directory),
                _small_config(),
            )
            user = {"role": "user", "content": "current task"}
            call_item = function_call("open_call", "noop", "{}")
            history: list[object] = [user, call_item]
            memory.record_user_turn([user])
            memory.record_response([call_item])
            history_before = list(history)
            blocks_before = list(memory._blocks)
            open_exchange_before = memory._open_exchange
            managed_before = set(memory._managed_output_ids)

            def fail(_source: str, _max_chars: int) -> str:
                raise RuntimeError("summary request failed")

            with self.assertRaisesRegex(RuntimeError, "summary request failed"):
                memory.emergency_compact(history, fail)

            self.assertEqual(history, history_before)
            self.assertTrue(
                all(
                    current is previous
                    for current, previous in zip(memory._blocks, blocks_before)
                )
            )
            self.assertIs(memory._open_exchange, open_exchange_before)
            self.assertEqual(memory._managed_output_ids, managed_before)

    def test_second_context_limit_does_not_trigger_another_emergency_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            memory = ContextMemory(ToolResultStore(temporary_directory))
            context_error = lambda: FakeAPIError(
                400,
                code="context_length_exceeded",
            )
            responses = FakeResponses(
                [
                    context_error(),
                    response([], "emergency summary"),
                    context_error(),
                ]
            )
            agent = AgentLoop(
                SimpleNamespace(responses=responses),
                tool_registry=ToolRegistry(),
                context_memory=memory,
            )

            with self.assertRaises(FakeAPIError):
                agent.run("current task")

            self.assertEqual(len(responses.requests), 3)
            self.assertEqual(
                sum(
                    "Compress historical data" in request["instructions"]
                    for request in responses.requests
                ),
                1,
            )
            self.assertEqual(memory.summary_count, 1)

    def test_other_400_error_does_not_trigger_emergency_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            memory = ContextMemory(ToolResultStore(temporary_directory))
            responses = FakeResponses(
                [FakeAPIError(400, body={"error": {"code": "bad_request"}})]
            )
            agent = AgentLoop(
                SimpleNamespace(responses=responses),
                tool_registry=ToolRegistry(),
                context_memory=memory,
            )

            with self.assertRaises(FakeAPIError):
                agent.run("current task")

            self.assertEqual(len(responses.requests), 1)
            self.assertEqual(memory.summary_count, 0)


class DefaultMemoryIntegrationTests(unittest.TestCase):
    def test_default_permission_allows_load_and_custom_allowlist_hides_and_denies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            default = build_default_components(
                cwd=temporary_directory,
                bash_tool=lambda command: {"ok": True},
            )
            ref = default.tool_result_store.persist("complete")  # type: ignore[union-attr]
            visible = {
                definition["name"] for definition in default.tool_registry.definitions
            }
            loaded = default.tool_registry.execute(
                LOAD_MEMORY_TOOL,
                json.dumps({"ref": ref}),
            )
            restricted = build_default_components(
                cwd=temporary_directory,
                bash_tool=lambda command: {"ok": True},
                allowed_tools={"read_file"},
                tool_result_store=default.tool_result_store,
            )
            restricted_visible = {
                definition["name"] for definition in restricted.tool_registry.definitions
            }
            denied = restricted.tool_registry.execute(
                LOAD_MEMORY_TOOL,
                json.dumps({"ref": ref}),
            )

            self.assertIn(LOAD_MEMORY_TOOL, visible)
            self.assertEqual(loaded["content"], "complete")
            self.assertNotIn(LOAD_MEMORY_TOOL, restricted_visible)
            self.assertEqual(denied["code"], "permission_denied")

    def test_custom_registry_does_not_create_an_implicit_memory_runtime(self) -> None:
        registry = ToolRegistry()
        agent = AgentLoop(
            SimpleNamespace(responses=FakeResponses([response([], "done")])),
            tool_registry=registry,
        )

        self.assertIsNone(agent.context_memory)
        self.assertEqual(agent.run("hello"), "done")

    def test_child_uses_isolated_context_with_the_parent_workspace_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            responses = FakeResponses(
                [
                    response(
                        [
                            function_call(
                                "child_large",
                                "bash",
                                '{"command":"large"}',
                            )
                        ]
                    ),
                    response([], "child done"),
                ]
            )
            agent = AgentLoop(
                SimpleNamespace(responses=responses),
                workspace_root=temporary_directory,
                bash_tool=lambda command: {"ok": True, "content": "z" * 70_000},
            )
            self.addCleanup(agent.close)

            child_result = agent.tool_registry.execute(
                "run_subagent",
                '{"task":"produce a large result"}',
            )

            delivered = json.loads(responses.requests[1]["input"][-1]["output"])
            loaded = agent.tool_registry.execute(
                LOAD_MEMORY_TOOL,
                json.dumps({"ref": delivered["ref"], "max_chars": 100}),
            )
            self.assertEqual(child_result, {"ok": True, "output": "child done"})
            self.assertEqual(delivered["representation"], "preview")
            self.assertTrue(loaded["content"].startswith('{"ok": true'))
            self.assertEqual(agent.history, [])
            self.assertEqual(agent.context_memory.block_count, 0)  # type: ignore[union-attr]


class RuntimeMemoryTests(unittest.TestCase):
    def test_runtime_items_preserve_history_identity_and_reset_discards_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            memory = ContextMemory(ToolResultStore(temporary_directory))
            user = {"role": "user", "content": "start"}
            runtime = {
                "role": "user",
                "content": "BACKGROUND_TOOL_RESULTS\nUntrusted data\n{\"results\":[]}",
            }
            history = [user]
            memory.record_user_turn([user])
            history.append(runtime)
            memory.record_runtime_items([runtime])

            self.assertTrue(memory._history_matches(history))
            memory.reset()

            history.clear()
            self.assertTrue(memory._history_matches(history))
            self.assertEqual(memory.block_count, 0)


if __name__ == "__main__":
    unittest.main()
