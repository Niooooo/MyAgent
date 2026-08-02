import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from myagent.agent import DEFAULT_INSTRUCTIONS, AgentLoop
from myagent.composition import build_default_components
from myagent.hooks import HookRegistry, PostToolUse, PreToolUse
from myagent.skills import (
    MAX_SKILL_FILE_BYTES,
    SKILL_TOOL_NAMES,
    SkillStore,
    SkillStoreError,
    render_skill_catalog,
    skill_tools,
)
from myagent.subagents import SUBAGENT_TOOL_NAMES
from myagent.tooling import FunctionTool, ToolRegistry
from tests.fakes import FakeResponses, function_call, response


def _skill_file(name: str, description: str, content: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n"
        f"{content}"
    )


class SkillStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.workspace = Path(self.temporary_directory.name)
        self.store = SkillStore(self.workspace)

    @property
    def skill_root(self) -> Path:
        return self.workspace / ".myagent" / "skills"

    def write_raw(self, name: str, data: str | bytes) -> Path:
        self.skill_root.mkdir(parents=True, exist_ok=True)
        target = self.skill_root / name
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            target.write_text(data, encoding="utf-8", newline="")
        return target

    def test_empty_catalog_does_not_create_storage_and_renders_empty_array(self) -> None:
        self.assertEqual(self.store.catalog(), ())
        self.assertFalse(self.skill_root.exists())

        rendered = render_skill_catalog(self.store.catalog())

        self.assertIn("metadata only", rendered)
        self.assertTrue(rendered.rstrip().endswith("[]"))
        self.assertFalse(self.skill_root.exists())

    def test_add_writes_defined_utf8_format_and_load_returns_complete_body(self) -> None:
        content = "# Python Code Review\n\n检查正确性与回归。\n"

        created = self.store.add(
            "python-code-review",
            "Review Python changes.",
            content,
        )
        target = self.skill_root / "python-code-review.md"
        loaded = self.store.load("python-code-review")

        self.assertEqual(created.name, "python-code-review")
        self.assertEqual(
            target.read_bytes().decode("utf-8"),
            _skill_file(
                "python-code-review",
                "Review Python changes.",
                content,
            ),
        )
        self.assertEqual(loaded.content, content)
        self.assertEqual(loaded.description, "Review Python changes.")

    def test_update_replaces_description_and_content_and_delete_removes_file(self) -> None:
        self.store.add("review", "Old description", "# Old")

        updated = self.store.update("review", "New description", "# New")

        self.assertEqual(updated.description, "New description")
        self.assertEqual(self.store.load("review").content, "# New")
        self.assertEqual(self.store.delete("review"), "review")
        self.assertFalse((self.skill_root / "review.md").exists())
        self.assertEqual(self.store.catalog(), ())

    def test_add_never_overwrites_and_update_delete_never_create(self) -> None:
        self.store.add("review", "Original", "# Original")

        with self.assertRaises(SkillStoreError) as duplicate:
            self.store.add("review", "Changed", "# Changed")
        with self.assertRaises(SkillStoreError) as missing_update:
            self.store.update("missing", "Description", "# Missing")
        with self.assertRaises(SkillStoreError) as missing_delete:
            self.store.delete("missing")

        self.assertEqual(duplicate.exception.code, "skill_exists")
        self.assertEqual(missing_update.exception.code, "skill_not_found")
        self.assertEqual(missing_delete.exception.code, "skill_not_found")
        self.assertEqual(self.store.load("review").content, "# Original")

    def test_name_validation_rejects_paths_absolute_names_case_and_controls(self) -> None:
        invalid_names = [
            "",
            "../escape",
            "/absolute",
            r"C:\escape",
            "nested/skill",
            r"nested\skill",
            "UPPER",
            ".hidden",
            "bad.name",
            "bad\x00name",
            "a" * 65,
        ]

        for name in invalid_names:
            with self.subTest(name=repr(name)):
                with self.assertRaises(SkillStoreError) as raised:
                    self.store.add(name, "Description", "# Content")
                self.assertEqual(raised.exception.code, "invalid_skill_name")

        self.assertFalse(self.skill_root.exists())

    def test_description_and_content_limits_use_text_and_utf8_bytes(self) -> None:
        limited = SkillStore(
            self.workspace,
            max_description_chars=5,
            max_content_bytes=5,
            max_file_bytes=128,
        )
        cases = [
            ("skill", "", "body", "invalid_skill_description"),
            ("skill", "line\nbreak", "body", "invalid_skill_description"),
            ("skill", "line\u2028break", "body", "invalid_skill_description"),
            ("skill", "123456", "body", "skill_description_too_long"),
            ("skill", "valid", "   ", "invalid_skill_content"),
            ("skill", "valid", "abc\x00d", "invalid_skill_content"),
            ("skill", "valid", "中文", "skill_content_too_large"),
        ]

        for name, description, content, code in cases:
            with self.subTest(code=code, content=repr(content)):
                with self.assertRaises(SkillStoreError) as raised:
                    limited.add(name, description, content)
                self.assertEqual(raised.exception.code, code)

        self.assertFalse(self.skill_root.exists())

    def test_load_rejects_damaged_front_matter_utf8_mismatch_and_oversize(self) -> None:
        cases: list[tuple[str | bytes, str]] = [
            ("# no front matter", "invalid_front_matter"),
            (b"\xff\xfe", "invalid_utf8"),
            (
                _skill_file("different", "Description", "# Body"),
                "skill_name_mismatch",
            ),
        ]
        for data, code in cases:
            with self.subTest(code=code):
                self.write_raw("broken.md", data)
                with self.assertRaises(SkillStoreError) as raised:
                    self.store.load("broken")
                self.assertEqual(raised.exception.code, code)

        self.write_raw("broken.md", b"x" * (MAX_SKILL_FILE_BYTES + 1))
        with self.assertRaises(SkillStoreError) as oversized:
            self.store.load("broken")
        self.assertEqual(oversized.exception.code, "skill_file_too_large")
        with self.assertRaises(SkillStoreError) as catalog_oversized:
            self.store.catalog()
        self.assertEqual(catalog_oversized.exception.code, "skill_file_too_large")

    def test_catalog_rejects_corruption_without_disclosing_raw_content(self) -> None:
        secret = "DO NOT DISCLOSE THIS RAW BODY"
        self.write_raw("broken.md", f"invalid\n{secret}")

        with self.assertRaises(SkillStoreError) as raised:
            self.store.catalog()

        self.assertEqual(raised.exception.code, "invalid_front_matter")
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(str(self.workspace), str(raised.exception))

    def test_catalog_contains_only_sorted_json_safe_metadata(self) -> None:
        self.store.add("zeta", "Zeta <metadata>", "# ZETA SECRET BODY")
        self.store.add("alpha", 'Alpha "metadata"', "# ALPHA SECRET BODY")

        rendered = render_skill_catalog(self.store.catalog())

        self.assertLess(rendered.index('"alpha"'), rendered.index('"zeta"'))
        self.assertIn('Alpha \\"metadata\\"', rendered)
        self.assertNotIn("SECRET BODY", rendered)
        self.assertNotIn(".myagent", rendered)

    def test_case_insensitive_storage_collision_is_rejected(self) -> None:
        self.write_raw(
            "Review.md",
            _skill_file("review", "Description", "# Body"),
        )

        with self.assertRaises(SkillStoreError) as load_error:
            self.store.load("review")
        with self.assertRaises(SkillStoreError) as add_error:
            self.store.add("review", "Description", "# New")

        self.assertEqual(load_error.exception.code, "skill_name_collision")
        self.assertEqual(add_error.exception.code, "skill_name_collision")

    def test_symlink_pointing_outside_skill_root_is_rejected(self) -> None:
        outside_directory = tempfile.TemporaryDirectory()
        self.addCleanup(outside_directory.cleanup)
        outside = Path(outside_directory.name, "escape.md")
        outside.write_text(
            _skill_file("escape", "Description", "# Secret"),
            encoding="utf-8",
        )
        self.skill_root.mkdir(parents=True)
        link = self.skill_root / "escape.md"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symbolic links are unavailable: {exc}")

        with self.assertRaises(SkillStoreError) as raised:
            self.store.load("escape")

        self.assertEqual(raised.exception.code, "unsafe_skill_file")
        self.assertNotIn(str(outside), str(raised.exception))

    def test_update_failure_preserves_old_file_and_cleans_temporary_file(self) -> None:
        self.store.add("review", "Original", "# Original")
        target = self.skill_root / "review.md"
        original = target.read_bytes()

        with patch("myagent.skills.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(SkillStoreError) as raised:
                self.store.update("review", "Updated", "# Updated")

        self.assertEqual(raised.exception.code, "skill_storage_error")
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(
            [path for path in self.skill_root.iterdir() if path.suffix == ".tmp"],
            [],
        )

    def test_total_limit_rejects_add_and_external_overflow_is_integrity_error(self) -> None:
        limited = SkillStore(self.workspace, max_skills=1)
        limited.add("first", "First", "# First")

        with self.assertRaises(SkillStoreError) as add_error:
            limited.add("second", "Second", "# Second")
        self.write_raw("second.md", _skill_file("second", "Second", "# Second"))
        with self.assertRaises(SkillStoreError) as catalog_error:
            limited.catalog()

        self.assertEqual(add_error.exception.code, "skill_limit_reached")
        self.assertEqual(catalog_error.exception.code, "skill_limit_exceeded")

    def test_skill_tools_return_stable_results_without_paths_or_redundant_content(self) -> None:
        registry = ToolRegistry(skill_tools(self.store))

        missing = registry.execute("load_skill", '{"name":"missing"}')
        malformed = registry.execute("load_skill", "not-json")
        created = registry.execute(
            "add_skill",
            json.dumps(
                {
                    "name": "review",
                    "description": "Review code",
                    "content": "# Secret body",
                }
            ),
        )
        loaded = registry.execute("load_skill", '{"name":"review"}')
        deleted = registry.execute("delete_skill", '{"name":"review"}')

        self.assertEqual(
            missing,
            {
                "ok": False,
                "code": "skill_not_found",
                "error": "Skill 'missing' does not exist",
            },
        )
        self.assertEqual(malformed["code"], "invalid_tool_arguments")
        self.assertTrue(created["created"])
        self.assertNotIn("content", created)
        self.assertEqual(loaded["content"], "# Secret body")
        self.assertTrue(deleted["deleted"])
        self.assertNotIn("content", deleted)
        self.assertNotIn(str(self.workspace), json.dumps([missing, created, deleted]))

    def test_concurrent_add_of_same_name_has_exactly_one_success(self) -> None:
        def add_once(index: int) -> str:
            try:
                self.store.add("shared", f"Description {index}", f"# Body {index}")
            except SkillStoreError as exc:
                return exc.code
            return "created"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(add_once, (1, 2)))

        self.assertCountEqual(results, ["created", "skill_exists"])
        self.assertIn(self.store.load("shared").content, {"# Body 1", "# Body 2"})

    def test_update_delete_race_returns_only_structured_outcomes(self) -> None:
        self.store.add("shared", "Original", "# Original")
        registry = ToolRegistry(skill_tools(self.store))
        barrier = threading.Barrier(2)

        def update() -> dict[str, object]:
            barrier.wait(2)
            return registry.execute(
                "update_skill",
                json.dumps(
                    {
                        "name": "shared",
                        "description": "Updated",
                        "content": "# Updated",
                    }
                ),
            )

        def delete() -> dict[str, object]:
            barrier.wait(2)
            return registry.execute("delete_skill", '{"name":"shared"}')

        with ThreadPoolExecutor(max_workers=2) as executor:
            update_future = executor.submit(update)
            delete_future = executor.submit(delete)
            updated = update_future.result()
            deleted = delete_future.result()

        self.assertTrue(deleted["ok"])
        self.assertTrue(updated["ok"] or updated["code"] == "skill_not_found")
        self.assertFalse((self.skill_root / "shared.md").exists())


class DynamicSkillInstructionsTests(unittest.TestCase):
    def test_corrupt_catalog_stops_before_api_request_without_injecting_raw_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory, ".myagent", "skills")
            root.mkdir(parents=True)
            secret = "RAW CORRUPT SECRET"
            (root / "broken.md").write_text(
                f"not front matter\n{secret}",
                encoding="utf-8",
            )
            responses = FakeResponses([])
            agent = AgentLoop(
                SimpleNamespace(responses=responses),
                workspace_root=temporary_directory,
            )
            self.addCleanup(agent.close)

            with self.assertRaises(SkillStoreError) as raised:
                agent.run("inspect")

            self.assertEqual(raised.exception.code, "invalid_front_matter")
            self.assertNotIn(secret, str(raised.exception))
            self.assertEqual(responses.requests, [])

    def test_generic_provider_refreshes_each_request_without_accumulating(self) -> None:
        fragments = iter(["dynamic one", "dynamic two"])
        registry = ToolRegistry(
            [
                FunctionTool(
                    name="noop",
                    description="No-op",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda: {"ok": True},
                )
            ]
        )
        responses = FakeResponses(
            [
                response([function_call("noop_call", "noop", "{}")]),
                response([], "done"),
            ]
        )
        agent = AgentLoop(
            SimpleNamespace(responses=responses),
            instructions="base",
            instructions_provider=lambda: next(fragments),
            tool_registry=registry,
        )

        self.assertEqual(agent.run("go"), "done")

        self.assertEqual(responses.requests[0]["instructions"], "base\n\ndynamic one")
        self.assertEqual(responses.requests[1]["instructions"], "base\n\ndynamic two")
        self.assertNotIn("dynamic one", responses.requests[1]["instructions"])
        self.assertEqual(agent.instructions, "base")

    def test_add_update_delete_refresh_next_model_request_and_never_add_body(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            calls = [
                function_call(
                    "add_call",
                    "add_skill",
                    json.dumps(
                        {
                            "name": "review",
                            "description": "First description",
                            "content": "# FIRST SECRET BODY",
                        }
                    ),
                ),
                function_call(
                    "update_call",
                    "update_skill",
                    json.dumps(
                        {
                            "name": "review",
                            "description": "Second description",
                            "content": "# SECOND SECRET BODY",
                        }
                    ),
                ),
                function_call("delete_call", "delete_skill", '{"name":"review"}'),
            ]
            responses = FakeResponses(
                [
                    response([calls[0]]),
                    response([calls[1]]),
                    response([calls[2]]),
                    response([], "done"),
                ]
            )
            approvals = []
            agent = AgentLoop(
                SimpleNamespace(responses=responses),
                workspace_root=temporary_directory,
                approval_callback=lambda request: approvals.append(request) or True,
            )
            self.addCleanup(agent.close)

            answer = agent.run("manage a skill")

            self.assertEqual(answer, "done")
            instructions = [request["instructions"] for request in responses.requests]
            self.assertIn("[]", instructions[0])
            self.assertIn("First description", instructions[1])
            self.assertNotIn("Second description", instructions[1])
            self.assertIn("Second description", instructions[2])
            self.assertNotIn("First description", instructions[2])
            self.assertIn("[]", instructions[3])
            self.assertTrue(
                all("SECRET BODY" not in instruction for instruction in instructions)
            )
            self.assertEqual([request.tool_name for request in approvals], [
                "add_skill",
                "update_skill",
                "delete_skill",
            ])
            outputs = [
                item
                for item in agent.history
                if isinstance(item, dict)
                and item.get("type") == "function_call_output"
            ]
            self.assertEqual(
                [item["call_id"] for item in outputs],
                ["add_call", "update_call", "delete_call"],
            )

    def test_load_result_enters_function_output_with_original_call_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            responses = FakeResponses(
                [
                    response(
                        [
                            function_call(
                                "load_original",
                                "load_skill",
                                '{"name":"review"}',
                            )
                        ]
                    ),
                    response([], "loaded"),
                ]
            )
            agent = AgentLoop(
                SimpleNamespace(responses=responses),
                workspace_root=temporary_directory,
            )
            self.addCleanup(agent.close)
            self.assertIsNotNone(agent.skill_store)
            agent.skill_store.add("review", "Review code", "# Complete body")

            self.assertEqual(agent.run("load it"), "loaded")

            output = responses.requests[1]["input"][-1]
            self.assertEqual(output["call_id"], "load_original")
            self.assertEqual(json.loads(output["output"])["content"], "# Complete body")

    def test_disabling_load_hides_catalog_and_custom_registry_creates_no_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            disabled_responses = FakeResponses([response([], "done")])
            disabled = AgentLoop(
                SimpleNamespace(responses=disabled_responses),
                workspace_root=temporary_directory,
                allowed_tools={"add_skill"},
            )
            self.addCleanup(disabled.close)

            self.assertEqual(disabled.run("answer"), "done")
            self.assertEqual(
                {item["name"] for item in disabled_responses.requests[0]["tools"]},
                {"add_skill"},
            )
            self.assertEqual(
                disabled_responses.requests[0]["instructions"],
                DEFAULT_INSTRUCTIONS,
            )

            custom_responses = FakeResponses([response([], "custom")])
            custom = AgentLoop(
                SimpleNamespace(responses=custom_responses),
                tool_registry=ToolRegistry(),
            )
            self.assertIsNone(custom.skill_store)
            self.assertEqual(custom.run("custom"), "custom")
            self.assertEqual(
                custom_responses.requests[0]["instructions"],
                DEFAULT_INSTRUCTIONS,
            )


class SkillPermissionAndHookTests(unittest.TestCase):
    def test_all_four_skill_tools_traverse_pre_and_post_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            hooks = HookRegistry()
            pre_names = []
            post_names = []
            hooks.register(PreToolUse, lambda event: pre_names.append(event.tool_name))
            hooks.register(PostToolUse, lambda event: post_names.append(event.tool_name))
            components = build_default_components(
                cwd=temporary_directory,
                hooks=hooks,
                approval_callback=lambda request: True,
            )
            registry = components.tool_registry

            registry.execute(
                "add_skill",
                json.dumps(
                    {
                        "name": "review",
                        "description": "Original",
                        "content": "# Original",
                    }
                ),
            )
            registry.execute("load_skill", '{"name":"review"}')
            registry.execute(
                "update_skill",
                json.dumps(
                    {
                        "name": "review",
                        "description": "Updated",
                        "content": "# Updated",
                    }
                ),
            )
            registry.execute("delete_skill", '{"name":"review"}')

            expected = ["add_skill", "load_skill", "update_skill", "delete_skill"]
            self.assertEqual(pre_names, expected)
            self.assertEqual(post_names, expected)

    def test_load_is_allowed_but_all_writes_require_fresh_full_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            requests = []
            answers = iter([True, False, True])
            components = build_default_components(
                cwd=temporary_directory,
                approval_callback=lambda request: requests.append(request)
                or next(answers),
            )
            registry = components.tool_registry

            created = registry.execute(
                "add_skill",
                json.dumps(
                    {
                        "name": "review",
                        "description": "Original",
                        "content": "# Original body",
                    }
                ),
            )
            loaded = registry.execute("load_skill", '{"name":"review"}')
            denied_update = registry.execute(
                "update_skill",
                json.dumps(
                    {
                        "name": "review",
                        "description": "Updated",
                        "content": "# Updated body",
                    }
                ),
            )
            deleted = registry.execute("delete_skill", '{"name":"review"}')

            self.assertTrue(created["ok"])
            self.assertEqual(loaded["content"], "# Original body")
            self.assertEqual(denied_update["code"], "permission_denied")
            self.assertTrue(deleted["ok"])
            self.assertEqual(
                [request.tool_name for request in requests],
                ["add_skill", "update_skill", "delete_skill"],
            )
            self.assertEqual(
                dict(requests[1].arguments),
                {
                    "name": "review",
                    "description": "Updated",
                    "content": "# Updated body",
                },
            )

    def test_missing_rejected_or_failed_approval_never_runs_handler(self) -> None:
        callbacks = [
            None,
            lambda request: False,
            lambda request: (_ for _ in ()).throw(RuntimeError("unavailable")),
        ]
        for index, callback in enumerate(callbacks):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                components = build_default_components(
                    cwd=directory,
                    approval_callback=callback,
                )
                result = components.tool_registry.execute(
                    "add_skill",
                    json.dumps(
                        {
                            "name": "review",
                            "description": "Description",
                            "content": "# Body",
                        }
                    ),
                )

                self.assertEqual(result["code"], "permission_denied")
                self.assertFalse(Path(directory, ".myagent", "skills").exists())

    def test_audit_hook_observes_rejected_permission_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            hooks = HookRegistry()
            observed = []
            hooks.register(
                PreToolUse,
                lambda event: observed.append(
                    (
                        event.permission_level,
                        event.permission_reason,
                        event.denial_reason,
                    )
                ),
            )
            components = build_default_components(cwd=temporary_directory, hooks=hooks)

            result = components.tool_registry.execute(
                "add_skill",
                json.dumps(
                    {
                        "name": "review",
                        "description": "Description",
                        "content": "# Body",
                    }
                ),
            )

            self.assertEqual(result["code"], "permission_denied")
            self.assertEqual(observed[0][0], "deny")
            self.assertIn("no user approval handler", observed[0][1])
            self.assertEqual(observed[0][1], observed[0][2])

    def test_permission_is_first_and_audit_hooks_see_decision_then_post_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            hooks = HookRegistry()
            observations = []

            def audit_pre(event: PreToolUse) -> None:
                observations.append(
                    (
                        "pre",
                        event.tool_name,
                        event.permission_level,
                        event.permission_reason,
                    )
                )

            def audit_post(event: PostToolUse) -> None:
                observations.append(("post", event.tool_name, event.result["ok"]))
                event.result["audited"] = True

            hooks.register(PreToolUse, audit_pre)
            hooks.register(PostToolUse, audit_post)
            components = build_default_components(
                cwd=temporary_directory,
                hooks=hooks,
                approval_callback=lambda request: True,
            )

            result = components.tool_registry.execute(
                "add_skill",
                json.dumps(
                    {
                        "name": "review",
                        "description": "Description",
                        "content": "# Body",
                    }
                ),
            )

            self.assertTrue(result["audited"])
            self.assertEqual(observations[0][0:3], ("pre", "add_skill", "allow"))
            self.assertEqual(observations[0][3], "approved by the user")
            self.assertEqual(observations[1], ("post", "add_skill", True))
            self.assertEqual(
                type(hooks.handlers_for(PreToolUse)[0]).__name__,
                "PermissionHook",
            )

    def test_allowlist_hides_and_rejects_each_disabled_skill_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            components = build_default_components(
                cwd=temporary_directory,
                allowed_tools={"read_file"},
            )
            visible = {
                definition["name"]
                for definition in components.tool_registry.definitions
            }

            self.assertEqual(visible, {"read_file"})
            self.assertIsNone(components.instructions_provider)
            for name in SKILL_TOOL_NAMES:
                raw_arguments = (
                    '{"name":"review"}'
                    if name in {"load_skill", "delete_skill"}
                    else json.dumps(
                        {
                            "name": "review",
                            "description": "Description",
                            "content": "# Body",
                        }
                    )
                )
                with self.subTest(name=name):
                    result = components.tool_registry.execute(name, raw_arguments)
                    self.assertEqual(result["code"], "permission_denied")

    def test_each_skill_tool_can_be_enabled_independently(self) -> None:
        for enabled in SKILL_TOOL_NAMES:
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as directory:
                components = build_default_components(
                    cwd=directory,
                    allowed_tools={enabled},
                )
                visible = {
                    definition["name"]
                    for definition in components.tool_registry.definitions
                }

                self.assertEqual(visible, {enabled})
                self.assertEqual(
                    components.instructions_provider is not None,
                    enabled == "load_skill",
                )


class ParentChildSkillIntegrationTests(unittest.TestCase):
    def test_parent_and_child_share_store_catalog_while_runtime_state_stays_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            responses = FakeResponses(
                [
                    response(
                        [
                            function_call(
                                "child_load",
                                "load_skill",
                                '{"name":"shared"}',
                            ),
                            function_call(
                                "child_update",
                                "update_skill",
                                json.dumps(
                                    {
                                        "name": "shared",
                                        "description": "Updated by child",
                                        "content": "# Child body",
                                    }
                                ),
                            )
                        ]
                    ),
                    response([], "child done"),
                    response([], "parent done"),
                ]
            )
            agent = AgentLoop(
                SimpleNamespace(responses=responses),
                workspace_root=temporary_directory,
                approval_callback=lambda request: True,
            )
            self.addCleanup(agent.close)
            created = agent.tool_registry.execute(
                "add_skill",
                json.dumps(
                    {
                        "name": "shared",
                        "description": "Created by parent",
                        "content": "# Parent body",
                    }
                ),
            )

            child_result = agent.tool_registry.execute(
                "run_subagent",
                '{"task":"update the shared skill"}',
            )
            parent_answer = agent.run("inspect the catalog")

            self.assertTrue(created["ok"])
            self.assertEqual(child_result, {"ok": True, "output": "child done"})
            self.assertEqual(parent_answer, "parent done")
            self.assertIn("Created by parent", responses.requests[0]["instructions"])
            self.assertIn("Updated by child", responses.requests[1]["instructions"])
            self.assertIn("Updated by child", responses.requests[2]["instructions"])
            child_outputs = [
                item
                for item in responses.requests[1]["input"]
                if isinstance(item, dict)
                and item.get("type") == "function_call_output"
            ]
            self.assertEqual(
                [item["call_id"] for item in child_outputs],
                ["child_load", "child_update"],
            )
            self.assertEqual(
                json.loads(child_outputs[0]["output"])["content"],
                "# Parent body",
            )
            child_tools = {
                definition["name"] for definition in responses.requests[0]["tools"]
            }
            self.assertTrue(SKILL_TOOL_NAMES.issubset(child_tools))
            self.assertTrue(SUBAGENT_TOOL_NAMES.isdisjoint(child_tools))
            self.assertEqual(
                agent.skill_store.load("shared").content,
                "# Child body",
            )
            self.assertEqual(
                responses.requests[0]["input"][0],
                {"role": "user", "content": "update the shared skill"},
            )
            self.assertEqual(
                agent.history[0],
                {"role": "user", "content": "inspect the catalog"},
            )

    def test_two_actual_forks_concurrently_add_same_skill_without_partial_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            endpoint = _ConcurrentAddResponses()
            agent = AgentLoop(
                SimpleNamespace(responses=endpoint),
                workspace_root=temporary_directory,
                approval_callback=lambda request: True,
            )
            self.addCleanup(agent.close)

            first = agent.tool_registry.execute(
                "fork_subagent",
                '{"task":"first fork"}',
            )
            second = agent.tool_registry.execute(
                "fork_subagent",
                '{"task":"second fork"}',
            )
            first_result = agent.tool_registry.execute(
                "collect_subagent",
                json.dumps(
                    {
                        "fork_id": first["fork_id"],
                        "wait": True,
                        "timeout_seconds": 3,
                    }
                ),
            )
            second_result = agent.tool_registry.execute(
                "collect_subagent",
                json.dumps(
                    {
                        "fork_id": second["fork_id"],
                        "wait": True,
                        "timeout_seconds": 3,
                    }
                ),
            )

            self.assertEqual(first_result["status"], "completed")
            self.assertEqual(second_result["status"], "completed")
            self.assertEqual(len(endpoint.tool_results), 2)
            self.assertEqual(
                sorted(
                    "created" if result["ok"] else result["code"]
                    for result in endpoint.tool_results
                ),
                ["created", "skill_exists"],
            )
            loaded = agent.skill_store.load("fork-shared")
            self.assertIn(loaded.content, {"# first fork", "# second fork"})
            raw = Path(
                temporary_directory,
                ".myagent",
                "skills",
                "fork-shared.md",
            ).read_text(encoding="utf-8")
            self.assertEqual(raw, _skill_file(
                "fork-shared",
                f"Created by {loaded.content.removeprefix('# ')}",
                loaded.content,
            ))


class _ConcurrentAddResponses:
    def __init__(self) -> None:
        self._first_request_barrier = threading.Barrier(2)
        self._lock = threading.Lock()
        self.tool_results: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> SimpleNamespace:
        history = kwargs["input"]
        function_outputs = [
            item
            for item in history
            if isinstance(item, dict)
            and item.get("type") == "function_call_output"
        ]
        if function_outputs:
            result = json.loads(function_outputs[-1]["output"])
            with self._lock:
                self.tool_results.append(result)
            return response([], "done")

        task = next(
            item["content"]
            for item in history
            if isinstance(item, dict) and item.get("role") == "user"
        )
        self._first_request_barrier.wait(3)
        return response(
            [
                function_call(
                    f"add_{task.replace(' ', '_')}",
                    "add_skill",
                    json.dumps(
                        {
                            "name": "fork-shared",
                            "description": f"Created by {task}",
                            "content": f"# {task}",
                        }
                    ),
                )
            ]
        )


if __name__ == "__main__":
    unittest.main()
