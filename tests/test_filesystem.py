import json
import os
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from myagent.filesystem import WorkspaceFiles, filesystem_tools
from myagent.tooling import FunctionTool, ToolExecutionError, ToolRegistry


class WorkspaceFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.workspace = WorkspaceFiles(self.root)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_write_and_read_file_range(self) -> None:
        written = self.workspace.write_file(
            "notes/example.txt",
            "first\nsecond\nthird\n",
        )
        read = self.workspace.read_file(
            "notes/example.txt",
            start_line=2,
            max_lines=1,
        )

        self.assertTrue(written["created"])
        self.assertEqual(written["path"], "notes/example.txt")
        self.assertEqual(read["content"], "second\n")
        self.assertEqual(read["end_line"], 2)
        self.assertEqual(read["total_lines"], 3)
        self.assertTrue(read["truncated"])

    def test_edit_requires_unique_text_unless_replace_all_is_set(self) -> None:
        target = self.root / "example.txt"
        target.write_text("old old", encoding="utf-8")

        with self.assertRaisesRegex(ToolExecutionError, "occurs 2 times"):
            self.workspace.edit_file("example.txt", "old", "new")

        result = self.workspace.edit_file(
            "example.txt",
            "old",
            "new",
            replace_all=True,
        )

        self.assertEqual(result["replacements"], 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "new new")

    def test_paths_and_patterns_cannot_escape_workspace(self) -> None:
        with self.assertRaisesRegex(ToolExecutionError, "escapes"):
            self.workspace.read_file("../outside.txt")
        with self.assertRaisesRegex(ToolExecutionError, "must be relative"):
            self.workspace.glob("../*.txt")

        outside = self.root.parent / "outside.txt"
        with self.assertRaisesRegex(ToolExecutionError, "escapes"):
            self.workspace.read_file(str(outside.resolve()))

    def test_symlink_cannot_escape_workspace(self) -> None:
        outside_directory = tempfile.TemporaryDirectory()
        self.addCleanup(outside_directory.cleanup)
        outside = Path(outside_directory.name, "outside.txt")
        outside.write_text("secret", encoding="utf-8")
        link = self.root / "link.txt"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symbolic links are unavailable: {exc}")

        with self.assertRaisesRegex(ToolExecutionError, "escapes"):
            self.workspace.read_file("link.txt")

    def test_read_and_search_limits_are_preserved(self) -> None:
        limited = WorkspaceFiles(self.root, max_read_bytes=4, max_results=1)
        (self.root / "large.txt").write_text("12345", encoding="utf-8")
        (self.root / "first.txt").write_text("hit", encoding="utf-8")
        (self.root / "second.txt").write_text("hit", encoding="utf-8")

        with self.assertRaisesRegex(ToolExecutionError, "read limit"):
            limited.read_file("large.txt")
        result = limited.grep("hit")

        self.assertEqual(result["count"], 1)
        self.assertTrue(result["truncated"])

    def test_grep_skips_repository_and_cache_directories(self) -> None:
        for directory in (".git", ".venv", "__pycache__"):
            ignored = self.root / directory
            ignored.mkdir()
            (ignored / "ignored.txt").write_text("needle", encoding="utf-8")
        (self.root / "included.txt").write_text("needle", encoding="utf-8")

        result = self.workspace.grep("needle")

        self.assertEqual(
            [match["path"] for match in result["matches"]],
            ["included.txt"],
        )

    def test_writes_replace_a_same_directory_temporary_file(self) -> None:
        real_replace = os.replace
        with patch("myagent.filesystem.os.replace", wraps=real_replace) as replace:
            self.workspace.write_file("nested/note.txt", "content")

        temporary_name, target_name = replace.call_args.args
        self.assertEqual(Path(temporary_name).parent, Path(target_name).parent)
        self.assertEqual(Path(target_name), self.root / "nested" / "note.txt")

    def test_glob_and_grep_return_structured_matches(self) -> None:
        source = self.root / "src"
        source.mkdir()
        (source / "first.py").write_text("print('Hello')\n", encoding="utf-8")
        (source / "second.py").write_text("# hello again\n", encoding="utf-8")
        (source / "ignored.txt").write_text("hello\n", encoding="utf-8")

        glob_result = self.workspace.glob("**/*.py")
        grep_result = self.workspace.grep(
            "hello",
            file_pattern="*.py",
            case_sensitive=False,
        )

        self.assertEqual(
            [match["path"] for match in glob_result["matches"]],
            ["src/first.py", "src/second.py"],
        )
        self.assertEqual(grep_result["count"], 2)
        self.assertEqual(grep_result["matches"][0]["line"], 1)
        self.assertEqual(grep_result["matches"][0]["column"], 8)

    def test_binary_files_are_skipped_by_grep(self) -> None:
        (self.root / "binary.bin").write_bytes(b"hello\x00world")

        result = self.workspace.grep("hello")

        self.assertEqual(result["matches"], [])
        self.assertEqual(result["skipped_files"], 1)


class ToolRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        workspace = WorkspaceFiles(self.temporary_directory.name)
        self.registry = ToolRegistry(filesystem_tools(workspace))

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_registry_returns_expected_errors_in_band(self) -> None:
        malformed = self.registry.execute("read_file", "not-json")
        invalid_regex = self.registry.execute(
            "grep",
            json.dumps({"pattern": "["}),
        )
        unknown = self.registry.execute("missing", "{}")

        self.assertIn("Invalid tool arguments", malformed["error"])
        self.assertIn("Invalid regular expression", invalid_regex["error"])
        self.assertEqual(unknown["error"], "Unknown tool: missing")

    def test_registry_rejects_missing_and_extra_arguments(self) -> None:
        missing = self.registry.execute("read_file", "{}")
        extra = self.registry.execute(
            "read_file",
            json.dumps({"path": "file.txt", "unexpected": True}),
        )

        self.assertIn("Invalid arguments for read_file", missing["error"])
        self.assertIn("Invalid arguments for read_file", extra["error"])

    def test_registry_rejects_non_object_arguments(self) -> None:
        result = self.registry.execute("read_file", '[]')

        self.assertEqual(
            result,
            {
                "ok": False,
                "error": "Invalid tool arguments: expected a JSON object",
            },
        )

    def test_registry_converts_non_object_handler_result_to_error(self) -> None:
        registry = ToolRegistry(
            [
                FunctionTool(
                    name="invalid_result",
                    description="Return an invalid result",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda: "not an object",  # type: ignore[arg-type]
                )
            ]
        )

        result = registry.execute("invalid_result", "{}")

        self.assertEqual(
            result,
            {
                "ok": False,
                "error": "invalid_result tool returned a non-object result",
            },
        )

    def test_pre_tool_hook_receives_read_only_arguments(self) -> None:
        from myagent.hooks import HookRegistry, PreToolUse

        hooks = HookRegistry()
        seen_argument_types = []

        def inspect_arguments(event: PreToolUse) -> None:
            seen_argument_types.append(type(event.arguments))
            with self.assertRaises(TypeError):
                event.arguments["path"] = "changed"  # type: ignore[index]

        hooks.register(PreToolUse, inspect_arguments)
        registry = ToolRegistry(
            filesystem_tools(WorkspaceFiles(self.temporary_directory.name)),
            hooks=hooks,
        )

        result = registry.execute("read_file", '{"path":"missing.txt"}')

        self.assertFalse(result["ok"])
        self.assertEqual(seen_argument_types, [MappingProxyType])


if __name__ == "__main__":
    unittest.main()
