"""Workspace-scoped file and search tools."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .tooling import FunctionTool, ToolExecutionError


_IGNORED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}


class WorkspaceFiles:
    """Perform UTF-8 file operations without escaping a workspace root."""

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        max_read_bytes: int = 1_000_000,
        max_results: int = 500,
    ) -> None:
        if max_read_bytes <= 0:
            raise ValueError("max_read_bytes must be positive")
        if max_results <= 0:
            raise ValueError("max_results must be positive")

        self.root = Path(root or Path.cwd()).resolve()
        self.max_read_bytes = max_read_bytes
        self.max_results = max_results

    def read_file(
        self,
        path: str,
        start_line: int = 1,
        max_lines: int = 400,
    ) -> dict[str, Any]:
        """Read a bounded range of lines from a UTF-8 text file."""
        if not isinstance(start_line, int) or isinstance(start_line, bool):
            raise ToolExecutionError("start_line must be an integer")
        if start_line < 1:
            raise ToolExecutionError("start_line must be at least 1")
        if not isinstance(max_lines, int) or isinstance(max_lines, bool):
            raise ToolExecutionError("max_lines must be an integer")
        if not 1 <= max_lines <= 2_000:
            raise ToolExecutionError("max_lines must be between 1 and 2000")

        target = self._existing_file(path)
        if target.stat().st_size > self.max_read_bytes:
            raise ToolExecutionError(
                f"File is larger than the {self.max_read_bytes}-byte read limit"
            )

        try:
            with target.open("r", encoding="utf-8", newline="") as stream:
                lines = stream.readlines()
        except UnicodeDecodeError as exc:
            raise ToolExecutionError(f"File is not valid UTF-8 text: {path}") from exc

        total_lines = len(lines)
        start_index = start_line - 1
        selected = lines[start_index : start_index + max_lines]
        end_line = start_index + len(selected) if selected else 0
        return {
            "ok": True,
            "path": self._relative(target),
            "content": "".join(selected),
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": total_lines,
            "truncated": end_line < total_lines,
        }

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        """Create or replace a UTF-8 text file, including parent directories."""
        if not isinstance(content, str):
            raise ToolExecutionError("content must be a string")

        target = self._resolve(path)
        if target.exists() and target.is_dir():
            raise ToolExecutionError(f"Path is a directory: {path}")

        created = not target.exists()
        self._atomic_write(target, content)
        return {
            "ok": True,
            "path": self._relative(target),
            "created": created,
            "characters_written": len(content),
        }

    def edit_file(
        self,
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
    ) -> dict[str, Any]:
        """Replace exact text, rejecting missing or ambiguous single edits."""
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            raise ToolExecutionError("old_text and new_text must be strings")
        if not isinstance(replace_all, bool):
            raise ToolExecutionError("replace_all must be a boolean")
        if not old_text:
            raise ToolExecutionError("old_text must not be empty")

        target = self._existing_file(path)
        if target.stat().st_size > self.max_read_bytes:
            raise ToolExecutionError(
                f"File is larger than the {self.max_read_bytes}-byte edit limit"
            )

        try:
            with target.open("r", encoding="utf-8", newline="") as stream:
                content = stream.read()
        except UnicodeDecodeError as exc:
            raise ToolExecutionError(f"File is not valid UTF-8 text: {path}") from exc

        occurrences = content.count(old_text)
        if occurrences == 0:
            raise ToolExecutionError("old_text was not found in the file")
        if occurrences > 1 and not replace_all:
            raise ToolExecutionError(
                f"old_text occurs {occurrences} times; provide more context or set replace_all"
            )

        replacements = occurrences if replace_all else 1
        updated = content.replace(old_text, new_text, -1 if replace_all else 1)
        self._atomic_write(target, updated)
        return {
            "ok": True,
            "path": self._relative(target),
            "replacements": replacements,
        }

    def glob(self, pattern: str, path: str = ".") -> dict[str, Any]:
        """Find workspace paths matching a relative glob pattern."""
        self._validate_pattern(pattern)
        base = self._resolve(path)
        if not base.is_dir():
            raise ToolExecutionError(f"Search path is not a directory: {path}")

        matches: list[dict[str, str]] = []
        truncated = False
        try:
            candidates = sorted(base.glob(pattern), key=lambda item: str(item).lower())
        except (OSError, ValueError) as exc:
            raise ToolExecutionError(f"Invalid glob pattern: {exc}") from exc

        for candidate in candidates:
            resolved = candidate.resolve()
            if not self._is_within_root(resolved):
                continue
            if len(matches) >= self.max_results:
                truncated = True
                break
            matches.append(
                {
                    "path": self._relative(resolved),
                    "type": "directory" if resolved.is_dir() else "file",
                }
            )

        return {
            "ok": True,
            "matches": matches,
            "count": len(matches),
            "truncated": truncated,
        }

    def grep(
        self,
        pattern: str,
        path: str = ".",
        file_pattern: str | None = None,
        case_sensitive: bool = True,
    ) -> dict[str, Any]:
        """Search UTF-8 text files with a Python regular expression."""
        if not isinstance(pattern, str):
            raise ToolExecutionError("pattern must be a string")
        if not isinstance(case_sensitive, bool):
            raise ToolExecutionError("case_sensitive must be a boolean")
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            expression = re.compile(pattern, flags)
        except re.error as exc:
            raise ToolExecutionError(f"Invalid regular expression: {exc}") from exc

        if file_pattern is not None:
            self._validate_pattern(file_pattern)
        base = self._resolve(path)
        if not base.exists():
            raise ToolExecutionError(f"Path does not exist: {path}")

        matches: list[dict[str, Any]] = []
        skipped_files = 0
        truncated = False
        for candidate in self._iter_search_files(base, file_pattern):
            try:
                if candidate.stat().st_size > self.max_read_bytes:
                    skipped_files += 1
                    continue
                data = candidate.read_bytes()
            except OSError:
                skipped_files += 1
                continue

            if b"\x00" in data:
                skipped_files += 1
                continue
            text = data.decode("utf-8", errors="replace")
            for line_number, line in enumerate(text.splitlines(), start=1):
                match = expression.search(line)
                if match is None:
                    continue
                if len(matches) >= self.max_results:
                    truncated = True
                    break
                snippet_start = max(0, match.start() - 100)
                snippet = line[snippet_start : snippet_start + 500]
                matches.append(
                    {
                        "path": self._relative(candidate),
                        "line": line_number,
                        "column": match.start() + 1,
                        "text": snippet,
                        "snippet_start_column": snippet_start + 1,
                    }
                )
            if truncated:
                break

        return {
            "ok": True,
            "matches": matches,
            "count": len(matches),
            "truncated": truncated,
            "skipped_files": skipped_files,
        }

    def _resolve(self, raw_path: str) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ToolExecutionError("path must be a non-empty string")
        if "\x00" in raw_path:
            raise ToolExecutionError("path contains a null byte")

        supplied = Path(raw_path)
        candidate = supplied if supplied.is_absolute() else self.root / supplied
        resolved = candidate.resolve()
        if not self._is_within_root(resolved):
            raise ToolExecutionError("Path escapes the workspace root")
        return resolved

    def _existing_file(self, raw_path: str) -> Path:
        target = self._resolve(raw_path)
        if not target.exists():
            raise ToolExecutionError(f"File does not exist: {raw_path}")
        if not target.is_file():
            raise ToolExecutionError(f"Path is not a file: {raw_path}")
        return target

    def _is_within_root(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
        except ValueError:
            return False
        return True

    def _relative(self, path: Path) -> str:
        relative = path.relative_to(self.root)
        return relative.as_posix() or "."

    def _validate_pattern(self, pattern: str) -> None:
        if not isinstance(pattern, str) or not pattern:
            raise ToolExecutionError("pattern must be a non-empty string")
        parsed = Path(pattern)
        if parsed.is_absolute() or ".." in parsed.parts:
            raise ToolExecutionError("pattern must be relative and stay within the workspace")

    def _iter_search_files(
        self, base: Path, file_pattern: str | None
    ) -> Iterator[Path]:
        if base.is_file():
            if file_pattern is None or base.match(file_pattern):
                yield base
            return
        if not base.is_dir():
            return

        for current_root, directory_names, file_names in os.walk(
            base,
            followlinks=False,
        ):
            directory_names[:] = sorted(
                (
                    name
                    for name in directory_names
                    if name not in _IGNORED_DIRECTORIES
                ),
                key=str.lower,
            )
            for file_name in sorted(file_names, key=str.lower):
                candidate = Path(current_root, file_name)
                try:
                    relative = candidate.relative_to(base)
                    resolved = candidate.resolve()
                except (OSError, ValueError):
                    continue
                if file_pattern is not None and not relative.match(file_pattern):
                    continue
                if resolved.is_file() and self._is_within_root(resolved):
                    yield resolved

    @staticmethod
    def _atomic_write(target: Path, content: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
            os.replace(temporary_name, target)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass
            raise


def filesystem_tools(workspace: WorkspaceFiles) -> list[FunctionTool]:
    """Build function-tool definitions bound to one workspace."""
    return [
        FunctionTool(
            name="read_file",
            description=(
                "Read UTF-8 text from a file inside the workspace. Use start_line "
                "and max_lines to read large files in chunks."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path."},
                    "start_line": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 1,
                    },
                    "max_lines": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 2000,
                        "default": 400,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=workspace.read_file,
        ),
        FunctionTool(
            name="write_file",
            description=(
                "Create or completely overwrite a UTF-8 text file inside the workspace. "
                "Missing parent directories are created."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path."},
                    "content": {"type": "string", "description": "Complete new file content."},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            handler=workspace.write_file,
        ),
        FunctionTool(
            name="edit_file",
            description=(
                "Replace exact text in a UTF-8 workspace file. By default old_text "
                "must occur exactly once; set replace_all only for intentional bulk edits."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path."},
                    "old_text": {"type": "string", "description": "Exact text to replace."},
                    "new_text": {"type": "string", "description": "Replacement text."},
                    "replace_all": {"type": "boolean", "default": False},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
            handler=workspace.edit_file,
        ),
        FunctionTool(
            name="glob",
            description=(
                "List files and directories matching a relative glob such as "
                "'src/**/*.py' inside the workspace."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Relative glob pattern."},
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative directory to search.",
                        "default": ".",
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            handler=workspace.glob,
        ),
        FunctionTool(
            name="grep",
            description=(
                "Search UTF-8 workspace files with a Python regular expression and "
                "return matching paths, lines, columns, and snippets."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regular expression."},
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file or directory.",
                        "default": ".",
                    },
                    "file_pattern": {
                        "type": ["string", "null"],
                        "description": "Optional filename glob such as '*.py'.",
                        "default": None,
                    },
                    "case_sensitive": {"type": "boolean", "default": True},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            handler=workspace.grep,
        ),
    ]
