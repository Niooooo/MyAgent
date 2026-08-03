import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from myagent.agent import AgentLoop
from myagent.composition import build_default_components
from myagent.hooks import HookRegistry, PostToolUse, PreToolUse
from myagent.long_term_memory import (
    DELETE_MEMORY_TOOL,
    EXTRACT_MEMORY_TOOL,
    GET_MEMORY_ORGANIZATION_STATUS_TOOL,
    LONG_TERM_MEMORY_DIRECTORY,
    LONG_TERM_MEMORY_SCHEMA_VERSION,
    LONG_TERM_MEMORY_TOOL_NAMES,
    MAX_MEMORY_CONTENT_CHARS,
    MAX_MEMORY_TAGS,
    ORGANIZE_MEMORY_TOOL,
    SEARCH_MEMORY_ENTRIES_TOOL,
    STORE_MEMORY_TOOL,
    UPDATE_MEMORY_TOOL,
    LongTermMemoryStore,
    LongTermMemoryStoreError,
    long_term_memory_tools,
)
from myagent.memory import LOAD_MEMORY_TOOL
from myagent.tooling import ToolRegistry
from tests.fakes import FakeResponses, function_call, response


def _proposal(index: int = 1) -> dict[str, object]:
    return {
        "title": f"Architecture note {index}",
        "summary": f"Summary for note {index}",
        "content": f"Complete body {index}: 你好 🙂",
        "tags": ["Architecture", f"note-{index}"],
    }


class LongTermMemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.workspace = Path(self.temporary_directory.name)
        self.store = LongTermMemoryStore(self.workspace)
        self.root = self.workspace / LONG_TERM_MEMORY_DIRECTORY
        self.catalog_path = self.root / "catalog.json"
        self.entries_root = self.root / "entries"

    def _stored_entry_path(self, memory_id: str) -> Path:
        return self.entries_root / f"{memory_id}.json"

    def _assert_store_error(self, code: str, callable_, *args, **kwargs) -> None:
        with self.assertRaises(LongTermMemoryStoreError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn(str(self.workspace), raised.exception.error)

    def test_empty_search_has_no_filesystem_side_effects(self) -> None:
        self.assertEqual(self.store.search("anything", limit=5), ())
        self.assertEqual(self.store.search("anything", limit=5), ())
        self.assertFalse(self.root.exists())

    def test_store_search_extract_persists_metadata_and_body_separately(self) -> None:
        metadata = self.store.store(**_proposal())

        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        entry = json.loads(
            self._stored_entry_path(metadata.id).read_text(encoding="utf-8")
        )
        exact = self.store.search(metadata.id, limit=10)
        by_text = self.store.search("architecture", limit=10)
        extracted = self.store.extract(metadata.id)

        self.assertRegex(metadata.id, r"^[0-9a-f]{32}$")
        self.assertEqual(catalog["schema_version"], LONG_TERM_MEMORY_SCHEMA_VERSION)
        self.assertEqual(catalog["entries"], [metadata.as_dict()])
        self.assertNotIn("content", self.catalog_path.read_text(encoding="utf-8"))
        self.assertEqual(entry["schema_version"], LONG_TERM_MEMORY_SCHEMA_VERSION)
        self.assertEqual(
            {key: entry[key] for key in metadata.as_dict()},
            metadata.as_dict(),
        )
        self.assertEqual(entry["content"], _proposal()["content"])
        self.assertEqual(exact, (metadata,))
        self.assertEqual(by_text, (metadata,))
        self.assertEqual(extracted.metadata, metadata)
        self.assertEqual(extracted.content, _proposal()["content"])
        self.assertEqual(extracted.offset, 0)
        self.assertEqual(extracted.next_offset, len(_proposal()["content"]))
        self.assertFalse(extracted.truncated)

    def test_segmented_extract_uses_character_offsets_and_hard_bounds(self) -> None:
        metadata = self.store.store(
            title="Unicode",
            summary="Segmented read",
            content="ab🙂你好cd",
            tags=[],
        )

        first = self.store.extract(metadata.id, offset=0, max_chars=3)
        second = self.store.extract(
            metadata.id,
            offset=first.next_offset,
            max_chars=3,
        )
        end = self.store.extract(metadata.id, offset=7, max_chars=3)

        self.assertEqual(first.content + second.content, "ab🙂你好c")
        self.assertTrue(first.truncated)
        self.assertTrue(second.truncated)
        self.assertEqual(end.content, "")
        self.assertEqual(end.next_offset, 7)
        self.assertFalse(end.truncated)
        self._assert_store_error(
            "invalid_memory_offset",
            self.store.extract,
            metadata.id,
            offset=8,
        )
        self._assert_store_error(
            "invalid_memory_max_chars",
            self.store.extract,
            metadata.id,
            max_chars=0,
        )

    def test_invalid_missing_corrupt_and_inconsistent_entries_are_distinct(
        self,
    ) -> None:
        self._assert_store_error("invalid_memory_id", self.store.extract, "../note")
        self._assert_store_error(
            "memory_not_found",
            self.store.extract,
            "0" * 32,
        )

        metadata = self.store.store(**_proposal())
        entry_path = self._stored_entry_path(metadata.id)
        entry_path.unlink()
        self._assert_store_error(
            "memory_entry_missing",
            self.store.extract,
            metadata.id,
        )

        entry = {
            "schema_version": LONG_TERM_MEMORY_SCHEMA_VERSION,
            **metadata.as_dict(),
            "summary": "different",
            "content": "Complete body 1: 你好 🙂",
        }
        entry_path.write_text(
            json.dumps(entry, ensure_ascii=False),
            encoding="utf-8",
        )
        self._assert_store_error(
            "memory_metadata_mismatch",
            self.store.extract,
            metadata.id,
        )

        self.catalog_path.write_text("{not json", encoding="utf-8")
        self._assert_store_error(
            "memory_catalog_corrupt",
            self.store.search,
            "anything",
        )

    def test_catalog_rejects_duplicate_ids_and_never_exposes_orphans(self) -> None:
        metadata = self.store.store(**_proposal())
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["entries"].append(dict(catalog["entries"][0]))
        self.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        self._assert_store_error(
            "memory_catalog_corrupt",
            self.store.search,
            "anything",
        )

        self.catalog_path.write_text(
            json.dumps(
                {
                    "schema_version": LONG_TERM_MEMORY_SCHEMA_VERSION,
                    "entries": [],
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.store.search(metadata.id), ())
        self._assert_store_error("memory_not_found", self.store.extract, metadata.id)
        self.assertTrue(self._stored_entry_path(metadata.id).exists())

    def test_text_tag_and_entry_count_limits_are_enforced(self) -> None:
        self._assert_store_error(
            "memory_content_too_long",
            self.store.store,
            title="title",
            summary="summary",
            content="x" * (MAX_MEMORY_CONTENT_CHARS + 1),
            tags=[],
        )
        self._assert_store_error(
            "memory_tag_limit_exceeded",
            self.store.store,
            title="title",
            summary="summary",
            content="content",
            tags=[str(index) for index in range(MAX_MEMORY_TAGS + 1)],
        )
        self.assertFalse(self.root.exists())

        limited = LongTermMemoryStore(self.workspace, max_entries=1)
        limited.store(**_proposal(1))
        self._assert_store_error(
            "memory_limit_reached",
            limited.store,
            **_proposal(2),
        )

    def test_catalog_commit_failure_keeps_old_authority_and_hides_new_entry(
        self,
    ) -> None:
        original = self.store.store(**_proposal(1))
        original_catalog = self.catalog_path.read_bytes()
        real_replace = os.replace

        def fail_catalog_replace(source, destination) -> None:
            if Path(destination).name == "catalog.json":
                raise OSError("private operating system detail")
            real_replace(source, destination)

        with mock.patch(
            "myagent.long_term_memory.os.replace",
            side_effect=fail_catalog_replace,
        ):
            self._assert_store_error(
                "memory_storage_error",
                self.store.store,
                **_proposal(2),
            )

        self.assertEqual(self.catalog_path.read_bytes(), original_catalog)
        self.assertEqual(self.store.search("Architecture note", limit=10), (original,))
        self.assertEqual(
            sorted(path.name for path in self.entries_root.glob("*.json")),
            [f"{original.id}.json"],
        )

    def test_repeated_reads_do_not_modify_persistent_state(self) -> None:
        metadata = self.store.store(**_proposal())
        paths = [self.catalog_path, self._stored_entry_path(metadata.id)]
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in paths
        }

        for _ in range(3):
            self.store.search("summary")
            self.store.extract(metadata.id, max_chars=4)

        after = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in paths
        }
        self.assertEqual(after, before)

    def test_same_title_and_concurrent_creates_get_unique_ids(self) -> None:
        def create(index: int):
            proposal = _proposal(index)
            proposal["title"] = "same title"
            return self.store.store(**proposal)

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(create, range(12)))

        ids = {result.id for result in results}
        self.assertEqual(len(ids), len(results))
        self.assertEqual(len(self.store.search("same title", limit=20)), len(results))

    def test_search_sorting_and_unicode_casefold_are_deterministic(self) -> None:
        timestamps = iter(
            [
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 1, 2, tzinfo=timezone.utc),
            ]
        )
        identifiers = iter(["1" * 32, "2" * 32])
        store = LongTermMemoryStore(
            self.workspace,
            clock=lambda: next(timestamps),
            id_factory=lambda: next(identifiers),
        )
        older = store.store(
            title="Straße common",
            summary="older",
            content="older body",
            tags=[],
        )
        newer = store.store(
            title="newer common",
            summary=f"mentions {older.id}",
            content="newer body",
            tags=["STRASSE"],
        )

        self.assertEqual(store.search("common"), (newer, older))
        self.assertEqual(store.search("straße"), (newer, older))
        self.assertEqual(store.search(older.id), (older, newer))

    def test_symlink_escape_is_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as outside_directory:
            outside = Path(outside_directory)
            metadata_root = self.workspace / ".myagent"
            metadata_root.mkdir()
            try:
                (metadata_root / "memories").symlink_to(
                    outside,
                    target_is_directory=True,
                )
            except OSError as exc:
                self.skipTest(f"symbolic links are unavailable: {exc}")

            self._assert_store_error(
                "unsafe_memory_storage",
                self.store.store,
                **_proposal(),
            )
            self.assertEqual(list(outside.iterdir()), [])


class LongTermMemoryToolTests(unittest.TestCase):
    def test_tools_return_stable_shapes_without_paths_or_full_store_echo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LongTermMemoryStore(directory)
            tools = {tool.name: tool for tool in long_term_memory_tools(store)}
            created = tools[STORE_MEMORY_TOOL].handler(**_proposal())
            searched = tools[SEARCH_MEMORY_ENTRIES_TOOL].handler(
                query="architecture",
                limit=10,
            )
            extracted = tools[EXTRACT_MEMORY_TOOL].handler(
                id=created["id"],
                offset=0,
                max_chars=8,
            )
            invalid = tools[SEARCH_MEMORY_ENTRIES_TOOL].handler(query="", limit=10)

            self.assertEqual(set(tools), LONG_TERM_MEMORY_TOOL_NAMES)
            self.assertTrue(created["created"])
            self.assertNotIn("content", created)
            self.assertNotIn("content", searched["entries"][0])
            self.assertEqual(extracted["content"], "Complete")
            self.assertTrue(extracted["truncated"])
            self.assertEqual(invalid["code"], "invalid_memory_query")
            self.assertNotIn(str(directory), json.dumps(invalid))


class LongTermMemoryPermissionAndCompositionTests(unittest.TestCase):
    def test_write_requires_fresh_full_approval_but_reads_are_default_allowed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            requests = []
            components = build_default_components(
                cwd=directory,
                approval_callback=lambda request: requests.append(request) or True,
            )
            registry = components.tool_registry

            first = registry.execute(STORE_MEMORY_TOOL, json.dumps(_proposal(1)))
            second = registry.execute(STORE_MEMORY_TOOL, json.dumps(_proposal(2)))
            searched = registry.execute(
                SEARCH_MEMORY_ENTRIES_TOOL,
                json.dumps({"query": "architecture", "limit": 10}),
            )
            extracted = registry.execute(
                EXTRACT_MEMORY_TOOL,
                json.dumps({"id": first["id"], "max_chars": 10}),
            )
            updated_arguments = {"id": first["id"], **_proposal(3)}
            updated = registry.execute(
                UPDATE_MEMORY_TOOL,
                json.dumps(updated_arguments),
            )
            status = registry.execute(
                GET_MEMORY_ORGANIZATION_STATUS_TOOL,
                "{}",
            )
            organize_arguments = {
                "plan": {"updates": [], "deletions": []}
            }
            organized = registry.execute(
                ORGANIZE_MEMORY_TOOL,
                json.dumps(organize_arguments),
            )
            deleted = registry.execute(
                DELETE_MEMORY_TOOL,
                json.dumps({"id": second["id"]}),
            )

            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            self.assertTrue(searched["ok"])
            self.assertTrue(extracted["ok"])
            self.assertTrue(updated["ok"])
            self.assertTrue(status["ok"])
            self.assertTrue(organized["ok"])
            self.assertTrue(deleted["ok"])
            self.assertEqual(
                [request.tool_name for request in requests],
                [
                    STORE_MEMORY_TOOL,
                    STORE_MEMORY_TOOL,
                    UPDATE_MEMORY_TOOL,
                    ORGANIZE_MEMORY_TOOL,
                    DELETE_MEMORY_TOOL,
                ],
            )
            self.assertEqual(dict(requests[0].arguments), _proposal(1))
            self.assertEqual(dict(requests[2].arguments), updated_arguments)
            self.assertEqual(dict(requests[3].arguments), organize_arguments)

        with tempfile.TemporaryDirectory() as denied_directory:
            denied = build_default_components(cwd=denied_directory)
            result = denied.tool_registry.execute(
                STORE_MEMORY_TOOL,
                json.dumps(_proposal()),
            )
            self.assertEqual(result["code"], "permission_denied")
            self.assertFalse(
                Path(denied_directory, LONG_TERM_MEMORY_DIRECTORY).exists()
            )

    def test_all_tools_traverse_hooks_and_preserve_call_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hooks = HookRegistry()
            pre = []
            post = []
            hooks.register(
                PreToolUse,
                lambda event: pre.append((event.tool_name, event.call_id)),
            )
            hooks.register(
                PostToolUse,
                lambda event: post.append((event.tool_name, event.call_id)),
            )
            components = build_default_components(
                cwd=directory,
                hooks=hooks,
                approval_callback=lambda request: True,
            )
            registry = components.tool_registry

            created = registry.execute(
                STORE_MEMORY_TOOL,
                json.dumps(_proposal()),
                call_id="store-call",
            )
            registry.execute(
                SEARCH_MEMORY_ENTRIES_TOOL,
                '{"query":"architecture"}',
                call_id="search-call",
            )
            registry.execute(
                EXTRACT_MEMORY_TOOL,
                json.dumps({"id": created["id"]}),
                call_id="extract-call",
            )
            registry.execute(
                UPDATE_MEMORY_TOOL,
                json.dumps({"id": created["id"], **_proposal(2)}),
                call_id="update-call",
            )
            registry.execute(
                GET_MEMORY_ORGANIZATION_STATUS_TOOL,
                "{}",
                call_id="status-call",
            )
            registry.execute(
                ORGANIZE_MEMORY_TOOL,
                '{"plan":{"updates":[],"deletions":[]}}',
                call_id="organize-call",
            )
            registry.execute(
                DELETE_MEMORY_TOOL,
                json.dumps({"id": created["id"]}),
                call_id="delete-call",
            )

            expected = [
                (STORE_MEMORY_TOOL, "store-call"),
                (SEARCH_MEMORY_ENTRIES_TOOL, "search-call"),
                (EXTRACT_MEMORY_TOOL, "extract-call"),
                (UPDATE_MEMORY_TOOL, "update-call"),
                (
                    GET_MEMORY_ORGANIZATION_STATUS_TOOL,
                    "status-call",
                ),
                (ORGANIZE_MEMORY_TOOL, "organize-call"),
                (DELETE_MEMORY_TOOL, "delete-call"),
            ]
            self.assertEqual(pre, expected)
            self.assertEqual(post, expected)
            self.assertEqual(
                type(hooks.handlers_for(PreToolUse)[0]).__name__,
                "PermissionHook",
            )

    def test_allowlist_hides_and_denies_each_long_term_memory_tool(self) -> None:
        arguments = {
            STORE_MEMORY_TOOL: json.dumps(_proposal()),
            SEARCH_MEMORY_ENTRIES_TOOL: '{"query":"anything"}',
            EXTRACT_MEMORY_TOOL: json.dumps({"id": "0" * 32}),
            UPDATE_MEMORY_TOOL: json.dumps(
                {"id": "0" * 32, **_proposal()}
            ),
            DELETE_MEMORY_TOOL: json.dumps({"id": "0" * 32}),
            GET_MEMORY_ORGANIZATION_STATUS_TOOL: "{}",
            ORGANIZE_MEMORY_TOOL: (
                '{"plan":{"updates":[],"deletions":[]}}'
            ),
        }
        for disabled in LONG_TERM_MEMORY_TOOL_NAMES:
            with (
                self.subTest(disabled=disabled),
                tempfile.TemporaryDirectory() as directory,
            ):
                enabled = LONG_TERM_MEMORY_TOOL_NAMES.difference({disabled})
                components = build_default_components(
                    cwd=directory,
                    allowed_tools=enabled,
                    approval_callback=lambda request: True,
                )
                visible = {
                    definition["name"]
                    for definition in components.tool_registry.definitions
                }
                forged = components.tool_registry.execute(
                    disabled,
                    arguments[disabled],
                )

                self.assertEqual(visible, enabled)
                self.assertEqual(forged["code"], "permission_denied")

    def test_parent_and_child_share_store_while_protocol_state_is_isolated(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            responses = FakeResponses(
                [
                    response(
                        [
                            function_call(
                                "child-store",
                                STORE_MEMORY_TOOL,
                                json.dumps(_proposal()),
                            )
                        ]
                    ),
                    response([], "child done"),
                ]
            )
            agent = AgentLoop(
                SimpleNamespace(responses=responses),
                workspace_root=directory,
                approval_callback=lambda request: True,
            )
            self.addCleanup(agent.close)

            child = agent.tool_registry.execute(
                "run_subagent",
                '{"task":"store the supplied memory"}',
            )
            found = agent.long_term_memory_store.search("architecture")

            self.assertEqual(child, {"ok": True, "output": "child done"})
            self.assertEqual(len(found), 1)
            self.assertEqual(agent.history, [])
            child_output = json.loads(
                responses.requests[1]["input"][-1]["output"]
            )
            self.assertEqual(
                responses.requests[1]["input"][-1]["call_id"],
                "child-store",
            )
            self.assertTrue(child_output["created"])
            child_tools = {
                definition["name"] for definition in responses.requests[0]["tools"]
            }
            self.assertTrue(LONG_TERM_MEMORY_TOOL_NAMES.issubset(child_tools))

    def test_explicit_store_is_exposed_and_custom_registry_creates_no_runtime(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LongTermMemoryStore(directory)
            components = build_default_components(
                cwd=directory,
                long_term_memory_store=store,
            )
            self.assertIs(components.long_term_memory_store, store)

            custom = AgentLoop(
                SimpleNamespace(responses=FakeResponses([response([], "done")])),
                tool_registry=ToolRegistry(),
            )
            self.assertIsNone(custom.long_term_memory_store)
            self.assertEqual(custom.run("hello"), "done")

    def test_existing_load_memory_tool_remains_visible(self) -> None:
        components = build_default_components()
        names = {
            definition["name"] for definition in components.tool_registry.definitions
        }
        self.assertIn(LOAD_MEMORY_TOOL, names)


if __name__ == "__main__":
    unittest.main()
