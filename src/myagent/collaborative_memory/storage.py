"""SQLite authority and guarded, rebuildable text/blob files."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path
from threading import RLock


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def identifier(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def project_identity(workspace: Path) -> tuple[Path, str]:
    """Git chooses the common repository; no model-controlled shell commands."""
    workspace = workspace.resolve()
    try:
        common = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout.strip()
        revision = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout.strip()
        directory = Path(common).resolve()
        if directory.name == ".git":
            return directory.parent, revision
    except (OSError, subprocess.SubprocessError):
        pass
    return workspace, "unversioned"


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT OR IGNORE INTO meta VALUES('version', 0);
CREATE TABLE IF NOT EXISTS episodes(
 id TEXT PRIMARY KEY, task TEXT NOT NULL, workspace TEXT NOT NULL, revision TEXT NOT NULL,
 criteria TEXT NOT NULL, lifecycle TEXT NOT NULL DEFAULT 'running',
 outcome TEXT NOT NULL DEFAULT 'unknown', version INTEGER NOT NULL DEFAULT 1,
 root_done INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
 agent_id TEXT NOT NULL, role TEXT NOT NULL, parent_run TEXT, task_id TEXT,
 state TEXT NOT NULL, workspace TEXT NOT NULL, revision TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS work(
 id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
 parent_run TEXT, task_id TEXT, state TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS events(
 id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
 run_id TEXT, agent_id TEXT NOT NULL, role TEXT NOT NULL, kind TEXT NOT NULL,
 body TEXT NOT NULL, blob TEXT, ordinal INTEGER NOT NULL,
 created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS events_episode ON events(episode_id, ordinal);
CREATE TABLE IF NOT EXISTS edges(
 source TEXT NOT NULL, target TEXT NOT NULL, kind TEXT NOT NULL,
 via TEXT NOT NULL, reason TEXT NOT NULL,
 PRIMARY KEY(source,target,kind,via));
CREATE TABLE IF NOT EXISTS evidence(
 id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
 criterion TEXT NOT NULL, passed INTEGER NOT NULL, source TEXT NOT NULL,
 summary TEXT NOT NULL, revision TEXT NOT NULL, event_id TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS insights(
 id TEXT PRIMARY KEY, text TEXT NOT NULL, conditions TEXT NOT NULL, roles TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active', version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS insight_sources(
 insight_id TEXT NOT NULL REFERENCES insights(id) ON DELETE CASCADE,
 episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
 event_ids TEXT NOT NULL, polarity TEXT NOT NULL, episode_version INTEGER NOT NULL,
 PRIMARY KEY(insight_id,episode_id,polarity,episode_version));
CREATE TABLE IF NOT EXISTS insight_versions(
 insight_id TEXT NOT NULL REFERENCES insights(id) ON DELETE CASCADE,
 version INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(insight_id,version));
CREATE TABLE IF NOT EXISTS usage(
 episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
 insight_id TEXT NOT NULL REFERENCES insights(id) ON DELETE CASCADE,
 stage TEXT NOT NULL, evidence_id TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(episode_id,insight_id,stage,evidence_id));
CREATE TABLE IF NOT EXISTS jobs(
 episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
 version INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
 lease_until REAL NOT NULL DEFAULT 0, owner TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(episode_id,version));
CREATE TABLE IF NOT EXISTS retrievals(
 id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
 role TEXT NOT NULL, body TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS tombstones(id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS blocked_learning(
 episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
 version INTEGER NOT NULL, PRIMARY KEY(episode_id,version));
"""


class Database:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.project, self.revision = project_identity(self.workspace)
        self.root = self.project / ".myagent" / "collaborative-memory"
        self._lock = RLock()
        self._guard(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path("projections").mkdir(exist_ok=True)
        self.path("blobs").mkdir(exist_ok=True)
        for suffix in ("", "-wal", "-shm", "-journal"):
            self.path("memory.sqlite3" + suffix)
        self.db = sqlite3.connect(self.path("memory.sqlite3"), timeout=2, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            schema_version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if schema_version not in {0, 1}:
                raise ValueError("unsupported collaborative memory schema version")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)
            self.db.execute("PRAGMA user_version=1")
        except BaseException:
            self.db.close()
            raise

    def _guard(self, path: Path) -> Path:
        # Reject links even when their current target happens to be inside root.
        current = path
        while current != self.project:
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                raise ValueError("memory paths must not contain links")
            if current == current.parent:
                raise ValueError("memory path outside project")
            current = current.parent
        if not path.resolve().is_relative_to(self.project):
            raise ValueError("memory path outside project")
        return path

    def path(self, name: str) -> Path:
        path = self.root / name
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("memory path outside corpus")
        return self._guard(path)

    @contextmanager
    def transaction(self):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            before = self.db.total_changes
            try:
                yield self.db
                if self.db.total_changes != before:
                    self.db.execute("UPDATE meta SET value=value+1 WHERE key='version'")
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self.db.execute(sql, args)]

    @property
    def version(self) -> int:
        return self.rows("SELECT value FROM meta WHERE key='version'")[0]["value"]

    def atomic_text(self, name: str, text: str) -> None:
        target = self.path(name)
        temporary = self.path(name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def content(self, text: str) -> tuple[str, str | None]:
        if len(text) <= 2000:
            return text, None
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        name = f"blobs/{digest}.txt"
        if not self.path(name).exists():
            self.atomic_text(name, text)
        return text[:2000], name

    def event_body(self, event: dict) -> str:
        if not event["blob"]:
            return event["body"]
        name = event["blob"]
        if not isinstance(name, str) or not name.startswith("blobs/") or len(name) != 74:
            raise ValueError("invalid memory content reference")
        with self.path(name).open("rb") as stream:
            data = stream.read(1_048_577)
        if len(data) > 1_048_576:
            raise ValueError("memory content exceeds the reference read limit")
        text = data.decode("utf-8")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != Path(name).stem:
            raise ValueError("corrupt memory content reference")
        return text

    def close(self) -> None:
        with self._lock:
            self.db.close()
