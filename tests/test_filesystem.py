import json
import tempfile
import unittest
from pathlib import Path

from myagent.filesystem import WorkspaceFiles, filesystem_tools
from myagent.tooling import ToolExecutionError, ToolRegistry


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


if __name__ == "__main__":
    unittest.main()
