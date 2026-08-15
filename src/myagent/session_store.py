"""Append-only JSONL persistence for durable conversation timelines."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .gui_conversations import ConversationStore, ConversationStoreError
from .session_timeline import SessionTimeline, TimelineOperation


SCHEMA_VERSION = 1


class SessionStoreError(RuntimeError):
    """A stable failure raised for invalid, stale, or unavailable session data."""


@dataclass(frozen=True)
class SessionMetadata:
    title: str
    workspace: str
    main_model: str = ""
    sub_model: str = ""
    kind: str = "main"

    def to_json(self) -> dict[str, str]:
        return {
            "title": self.title,
            "workspace": self.workspace,
            "mainModel": self.main_model,
            "subModel": self.sub_model,
            "kind": self.kind,
        }

    @classmethod
    def from_value(cls, value: object) -> SessionMetadata:
        if isinstance(value, cls):
            value = value.to_json()
        if not isinstance(value, Mapping):
            raise SessionStoreError("session metadata must be an object")
        fields = {"title", "workspace", "mainModel", "subModel", "kind"}
        if set(value) != fields:
            raise SessionStoreError("session metadata fields are incomplete or unknown")
        title = value["title"]
        workspace = value["workspace"]
        main_model = value["mainModel"]
        sub_model = value["subModel"]
        kind = value["kind"]
        if not isinstance(title, str) or not title.strip() or len(title) > 80:
            raise SessionStoreError("metadata.title must contain 1 to 80 characters")
        if (
            not isinstance(workspace, str)
            or not workspace
            or not Path(workspace).is_absolute()
        ):
            raise SessionStoreError("metadata.workspace must be an absolute path")
        if not isinstance(main_model, str) or len(main_model) > 120:
            raise SessionStoreError("metadata.mainModel must be a model name string")
        if not isinstance(sub_model, str) or len(sub_model) > 120:
            raise SessionStoreError("metadata.subModel must be a model name string")
        if kind not in {"main", "sub"}:
            raise SessionStoreError("metadata.kind is invalid")
        return cls(title, workspace, main_model, sub_model, kind)


@dataclass(frozen=True)
class CatalogSession:
    id: int
    created_at: str
    metadata: SessionMetadata


@dataclass(frozen=True)
class CatalogState:
    sessions: tuple[CatalogSession, ...]
    active_session_id: int | None
    next_session_id: int
    revision: int
    legacy_migrated: bool = False


@dataclass(frozen=True)
class StoredSession:
    id: int
    created_at: str
    metadata: SessionMetadata
    messages: tuple[dict[str, str], ...]
    timeline: SessionTimeline
    revision: int


@dataclass(frozen=True)
class AppendTurnResult:
    """The durable boundary a caller may acknowledge after a successful append."""

    new_revision: int
    through_operation_id: int


def default_session_store_root() -> Path:
    """Return the repository-external ``conversations`` directory."""
    override = os.getenv("MYAGENT_HOME") or os.getenv("MYAGENT_GUI_HOME")
    if override:
        home = Path(override).expanduser().resolve()
    else:
        appdata = os.getenv("APPDATA")
        home = (
            Path(appdata).expanduser().resolve() / "MyAgent"
            if appdata
            else Path.home() / "AppData" / "Roaming" / "MyAgent"
        )
    return home / "conversations"


class _StoreLock:
    """Small blocking advisory lock shared by every process using one root."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._stream: Any = None

    def __enter__(self) -> _StoreLock:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = self._path.open("a+b")
            self._stream.seek(0, os.SEEK_END)
            if self._stream.tell() == 0:
                self._stream.write(b"\0")
                self._stream.flush()
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX)
            return self
        except OSError as exc:
            if self._stream is not None:
                self._stream.close()
            raise SessionStoreError(f"unable to acquire session store lock: {exc}") from None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._stream is None:
            return
        try:
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()


class SessionStore:
    """Persist catalog state and per-session timeline deltas as strict JSONL."""

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        sensitive_values: Sequence[str] = (),
        legacy_path: str | os.PathLike[str] | None = None,
        auto_migrate: bool = True,
    ) -> None:
        self.root = Path(root) if root is not None else default_session_store_root()
        self.catalog_path = self.root / "catalog.jsonl"
        self.legacy_path = (
            Path(legacy_path)
            if legacy_path is not None
            else self.root.parent / "conversations.json"
        )
        self._lock_path = self.root / ".store.lock"
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sensitive_values = tuple(
            value for value in sensitive_values if isinstance(value, str) and value
        )
        self._auto_migrate = auto_migrate

    def initialize(
        self,
        *,
        migrate_legacy: bool | None = None,
        sensitive_values: Sequence[str] = (),
    ) -> CatalogState:
        with _StoreLock(self._lock_path):
            self.root.mkdir(parents=True, exist_ok=True)
            should_migrate = self._auto_migrate if migrate_legacy is None else migrate_legacy
            if should_migrate and self._migrate_legacy_locked(sensitive_values):
                return self._read_catalog_locked()[1]
            self._ensure_catalog_locked(sensitive_values)
            return self._read_catalog_locked()[1]

    def migrate_legacy(self, *, sensitive_values: Sequence[str] = ()) -> bool:
        with _StoreLock(self._lock_path):
            self.root.mkdir(parents=True, exist_ok=True)
            migrated = self._migrate_legacy_locked(sensitive_values)
            if not migrated:
                self._ensure_catalog_locked(sensitive_values)
            return migrated

    def load_catalog(self) -> CatalogState:
        with _StoreLock(self._lock_path):
            if not self.catalog_path.exists():
                return CatalogState((), None, 1, 0, False)
            _, state = self._read_catalog_locked()
            current: list[CatalogSession] = []
            for summary in state.sessions:
                stored = self._read_session_locked(summary.id)
                if stored.created_at != summary.created_at:
                    raise SessionStoreError(
                        f"catalog/session created_at mismatch for session {summary.id}"
                    )
                current.append(
                    CatalogSession(summary.id, summary.created_at, stored.metadata)
                )
            return CatalogState(
                tuple(current),
                state.active_session_id,
                state.next_session_id,
                state.revision,
                state.legacy_migrated,
            )

    def list_sessions(self) -> tuple[CatalogSession, ...]:
        return self.load_catalog().sessions

    def create_session(
        self,
        metadata: SessionMetadata | Mapping[str, object],
        *,
        sensitive_values: Sequence[str] = (),
    ) -> StoredSession:
        selected = SessionMetadata.from_value(metadata)
        secrets = self._secrets(sensitive_values)
        with _StoreLock(self._lock_path):
            self.root.mkdir(parents=True, exist_ok=True)
            self._prepare_catalog_locked(secrets)
            records, state = self._read_catalog_locked()
            session_id = state.next_session_id
            created_at = self._timestamp()
            header = self._session_header(session_id, created_at, selected)
            session_path = self._session_path(session_id)
            if session_path.exists():
                raise SessionStoreError(f"session file already exists: {session_id}")
            next_revision = state.revision + 1
            created = {
                "version": SCHEMA_VERSION,
                "event": "session_created",
                "timestamp": created_at,
                "revision": next_revision,
                "session_id": session_id,
                "created_at": created_at,
                "metadata": selected.to_json(),
            }
            activated = {
                "version": SCHEMA_VERSION,
                "event": "session_activated",
                "timestamp": created_at,
                "revision": next_revision + 1,
                "session_id": session_id,
            }
            self._parse_catalog_records([*records, created, activated])
            session_data = self._records_bytes([header], secrets)
            catalog_data = self._records_bytes([created, activated], secrets)
            self._atomic_write(session_path, session_data)
            try:
                self._append_bytes(self.catalog_path, catalog_data)
            except SessionStoreError:
                try:
                    session_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            return self._read_session_locked(session_id)

    def activate_session(
        self,
        session_id: int,
        *,
        sensitive_values: Sequence[str] = (),
    ) -> CatalogState:
        selected_id = _positive_int(session_id, "session_id")
        secrets = self._secrets(sensitive_values)
        with _StoreLock(self._lock_path):
            self._prepare_catalog_locked(secrets)
            records, state = self._read_catalog_locked()
            if selected_id not in {session.id for session in state.sessions}:
                raise SessionStoreError(f"session does not exist: {selected_id}")
            self._read_session_file(selected_id)
            event = {
                "version": SCHEMA_VERSION,
                "event": "session_activated",
                "timestamp": self._timestamp(),
                "revision": state.revision + 1,
                "session_id": selected_id,
            }
            _, next_state = self._parse_catalog_records([*records, event])
            self._append_bytes(
                self.catalog_path,
                self._records_bytes([event], secrets),
            )
            return next_state

    def load_session(self, session_id: int) -> StoredSession:
        selected_id = _positive_int(session_id, "session_id")
        with _StoreLock(self._lock_path):
            if self.catalog_path.exists():
                _, state = self._read_catalog_locked()
                if selected_id not in {session.id for session in state.sessions}:
                    raise SessionStoreError(f"session does not exist: {selected_id}")
            return self._read_session_locked(selected_id)

    def append_metadata(
        self,
        session_id: int,
        metadata: SessionMetadata | Mapping[str, object],
        *,
        expected_revision: int,
        sensitive_values: Sequence[str] = (),
    ) -> int:
        selected_id = _positive_int(session_id, "session_id")
        selected = SessionMetadata.from_value(metadata)
        expected = _nonnegative_int(expected_revision, "expected_revision")
        with _StoreLock(self._lock_path):
            records, stored = self._read_session_file(selected_id)
            if stored.revision != expected:
                raise SessionStoreError(
                    f"stale session revision: expected {expected}, actual {stored.revision}"
                )
            event = {
                "version": SCHEMA_VERSION,
                "event": "metadata_changed",
                "timestamp": self._timestamp(),
                "revision": expected + 1,
                "session_id": selected_id,
                "metadata": selected.to_json(),
            }
            self._parse_session_records([*records, event], selected_id)
            self._append_bytes(
                self._session_path(selected_id),
                self._records_bytes([event], self._secrets(sensitive_values)),
            )
            return expected + 1

    def append_turn(
        self,
        session_id: int,
        messages: Sequence[Mapping[str, object]],
        *,
        expected_revision: int,
        pending_operations: Sequence[TimelineOperation] | None = None,
        timeline: SessionTimeline | None = None,
        sensitive_values: Sequence[str] = (),
    ) -> AppendTurnResult:
        """Append one delta; the caller acknowledges its timeline only on success."""
        selected_id = _positive_int(session_id, "session_id")
        expected = _nonnegative_int(expected_revision, "expected_revision")
        if (pending_operations is None) == (timeline is None):
            raise SessionStoreError(
                "provide exactly one of pending_operations or timeline"
            )
        pending = (
            tuple(timeline.pending_operations)
            if timeline is not None
            else tuple(pending_operations or ())
        )
        if not pending:
            raise SessionStoreError("a committed turn must contain pending operations")
        normalized_messages = _messages_json(messages)
        with _StoreLock(self._lock_path):
            records, stored = self._read_session_file(selected_id)
            if stored.revision != expected:
                raise SessionStoreError(
                    f"stale session revision: expected {expected}, actual {stored.revision}"
                )
            operation_records = [_operation_json(operation) for operation in pending]
            durable_operations = tuple(
                _operation_from_json(record, "pending operation")
                for record in operation_records
            )
            existing = stored.timeline.operations
            try:
                SessionTimeline.from_operations((*existing, *durable_operations))
            except (TypeError, ValueError) as exc:
                raise SessionStoreError(f"invalid pending timeline operations: {exc}") from None
            event = {
                "version": SCHEMA_VERSION,
                "event": "turn_committed",
                "timestamp": self._timestamp(),
                "revision": expected + 1,
                "session_id": selected_id,
                "operations": operation_records,
                "messages": normalized_messages,
            }
            self._parse_session_records([*records, event], selected_id)
            self._append_bytes(
                self._session_path(selected_id),
                self._records_bytes([event], self._secrets(sensitive_values)),
            )
            return AppendTurnResult(expected + 1, durable_operations[-1].operation_id)

    def delete_session(
        self,
        session_id: int,
        *,
        expected_revision: int | None = None,
        activate_session_id: int | None = None,
    ) -> CatalogState:
        selected_id = _positive_int(session_id, "session_id")
        expected = (
            None
            if expected_revision is None
            else _nonnegative_int(expected_revision, "expected_revision")
        )
        with _StoreLock(self._lock_path):
            records, state = self._read_catalog_locked()
            if selected_id not in {session.id for session in state.sessions}:
                raise SessionStoreError(f"session does not exist: {selected_id}")
            _, stored = self._read_session_file(selected_id)
            if expected is not None and stored.revision != expected:
                raise SessionStoreError(
                    f"stale session revision: expected {expected}, actual {stored.revision}"
                )
            event = {
                "version": SCHEMA_VERSION,
                "event": "session_deleted",
                "timestamp": self._timestamp(),
                "revision": state.revision + 1,
                "session_id": selected_id,
            }
            catalog_events = [event]
            if activate_session_id is not None:
                replacement = _positive_int(activate_session_id, "activate_session_id")
                activated = {
                    "version": SCHEMA_VERSION,
                    "event": "session_activated",
                    "timestamp": self._timestamp(),
                    "revision": state.revision + 2,
                    "session_id": replacement,
                }
                catalog_events.append(activated)
            _, next_state = self._parse_catalog_records([*records, *catalog_events])
            path = self._session_path(selected_id)
            original = path.read_bytes()
            try:
                path.unlink()
            except OSError as exc:
                raise SessionStoreError(f"unable to delete session {selected_id}: {exc}") from None
            try:
                self._append_bytes(
                    self.catalog_path,
                    self._records_bytes(catalog_events, ()),
                )
            except SessionStoreError:
                try:
                    self._atomic_write(path, original)
                except SessionStoreError:
                    pass
                raise
            return next_state

    def _prepare_catalog_locked(
        self,
        sensitive_values: Sequence[str] = (),
    ) -> None:
        if self._auto_migrate and self._migrate_legacy_locked(sensitive_values):
            return
        self._ensure_catalog_locked(sensitive_values)

    def _ensure_catalog_locked(
        self,
        sensitive_values: Sequence[str] = (),
    ) -> None:
        if self.catalog_path.exists():
            self._read_catalog_locked()
            return
        header = self._catalog_header(self._timestamp())
        self._atomic_write(
            self.catalog_path,
            self._records_bytes([header], self._secrets(sensitive_values)),
        )

    def _migrate_legacy_locked(
        self,
        sensitive_values: Sequence[str] = (),
    ) -> bool:
        records: list[dict[str, Any]]
        state: CatalogState
        if self.catalog_path.exists():
            records, state = self._read_catalog_locked()
            if state.sessions or state.legacy_migrated:
                return False
        else:
            records = []
            state = CatalogState((), None, 1, 0, False)
        if not self.legacy_path.exists():
            return False
        try:
            legacy = ConversationStore(self.legacy_path).load()
        except ConversationStoreError as exc:
            raise SessionStoreError(f"legacy conversation data is invalid: {exc}") from None
        if legacy is None:
            return False
        secrets = self._secrets(sensitive_values)
        now = self._timestamp()
        catalog_records = records or [self._catalog_header(now)]
        revision = state.revision
        session_payloads: list[tuple[Path, bytes]] = []
        for legacy_session in legacy["sessions"]:
            metadata = SessionMetadata.from_value({
                "title": legacy_session["title"],
                "workspace": legacy_session["workspace"],
                "mainModel": legacy_session["mainModel"],
                "subModel": legacy_session["subModel"],
                "kind": legacy_session["kind"],
            })
            session_id = legacy_session["id"]
            timeline = SessionTimeline()
            timeline.restore_history(legacy_session["history"])
            operations = [_operation_json(item) for item in timeline.operations]
            header = self._session_header(session_id, now, metadata)
            committed = {
                "version": SCHEMA_VERSION,
                "event": "turn_committed",
                "timestamp": now,
                "revision": 1,
                "session_id": session_id,
                "operations": operations,
                "messages": _messages_json(legacy_session["messages"]),
            }
            self._parse_session_records([header, committed], session_id)
            session_payloads.append((
                self._session_path(session_id),
                self._records_bytes([header, committed], secrets),
            ))
            revision += 1
            catalog_records.append({
                "version": SCHEMA_VERSION,
                "event": "session_created",
                "timestamp": now,
                "revision": revision,
                "session_id": session_id,
                "created_at": now,
                "metadata": metadata.to_json(),
            })
        revision += 1
        catalog_records.append({
            "version": SCHEMA_VERSION,
            "event": "session_activated",
            "timestamp": now,
            "revision": revision,
            "session_id": legacy["activeSessionId"],
        })
        revision += 1
        catalog_records.append({
            "version": SCHEMA_VERSION,
            "event": "legacy_migrated",
            "timestamp": now,
            "revision": revision,
            "next_session_id": legacy["nextSessionId"],
        })
        self._parse_catalog_records(catalog_records)
        catalog_payload = self._records_bytes(catalog_records, secrets)

        temporary_files: list[tuple[Path, Path]] = []
        try:
            for target, payload in session_payloads:
                temporary_files.append((target, self._write_temporary(target, payload)))
            catalog_temp = self._write_temporary(self.catalog_path, catalog_payload)
            temporary_files.append((self.catalog_path, catalog_temp))
            for target, temporary in temporary_files[:-1]:
                os.replace(temporary, target)
            os.replace(catalog_temp, self.catalog_path)
        except OSError as exc:
            raise SessionStoreError(f"unable to commit legacy migration proposal: {exc}") from None
        finally:
            for _, temporary in temporary_files:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return True

    def _read_catalog_locked(self) -> tuple[list[dict[str, Any]], CatalogState]:
        records = self._read_records(self.catalog_path)
        return records, self._parse_catalog_records(records)[1]

    def _parse_catalog_records(
        self, records: Sequence[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], CatalogState]:
        if not records:
            raise SessionStoreError("catalog JSONL is empty")
        header = records[0]
        _exact_fields(
            header,
            {"version", "event", "timestamp", "revision", "next_session_id", "active_session_id"},
            "catalog header",
        )
        _version(header["version"], "catalog header")
        if header["event"] != "catalog_header":
            raise SessionStoreError("catalog first event must be catalog_header")
        _timestamp_value(header["timestamp"], "catalog header timestamp")
        if (
            type(header["revision"]) is not int
            or header["revision"] != 0
            or type(header["next_session_id"]) is not int
            or header["next_session_id"] != 1
        ):
            raise SessionStoreError("catalog header revision/next_session_id is invalid")
        if header["active_session_id"] is not None:
            raise SessionStoreError("catalog header active_session_id must be null")
        sessions: dict[int, CatalogSession] = {}
        active: int | None = None
        next_id = 1
        migrated = False
        expected_revision = 1
        for index, event in enumerate(records[1:], 2):
            kind = event.get("event") if isinstance(event, dict) else None
            common = {"version", "event", "timestamp", "revision"}
            if kind == "session_created":
                _exact_fields(event, common | {"session_id", "created_at", "metadata"}, f"catalog line {index}")
            elif kind in {"session_activated", "session_deleted"}:
                _exact_fields(event, common | {"session_id"}, f"catalog line {index}")
            elif kind == "legacy_migrated":
                _exact_fields(event, common | {"next_session_id"}, f"catalog line {index}")
            else:
                raise SessionStoreError(f"catalog line {index} has an unknown event")
            _version(event["version"], f"catalog line {index}")
            _timestamp_value(event["timestamp"], f"catalog line {index} timestamp")
            if type(event["revision"]) is not int or event["revision"] != expected_revision:
                raise SessionStoreError(f"catalog revision is not consecutive at line {index}")
            expected_revision += 1
            if kind == "session_created":
                session_id = _positive_int(event["session_id"], "catalog session_id")
                if session_id in sessions:
                    raise SessionStoreError("catalog creates a duplicate live session")
                created_at = _timestamp_value(event["created_at"], "created_at")
                metadata = SessionMetadata.from_value(event["metadata"])
                sessions[session_id] = CatalogSession(session_id, created_at, metadata)
                next_id = max(next_id, session_id + 1)
            elif kind == "session_activated":
                session_id = _positive_int(event["session_id"], "catalog session_id")
                if session_id not in sessions:
                    raise SessionStoreError("catalog activates a missing or deleted session")
                active = session_id
            elif kind == "session_deleted":
                session_id = _positive_int(event["session_id"], "catalog session_id")
                if session_id not in sessions:
                    raise SessionStoreError("catalog deletes a missing session")
                del sessions[session_id]
                if active == session_id:
                    active = None
            else:
                if migrated:
                    raise SessionStoreError("catalog contains duplicate legacy migration markers")
                selected_next = _positive_int(event["next_session_id"], "next_session_id")
                if selected_next <= max(sessions, default=0):
                    raise SessionStoreError("legacy next_session_id does not exceed session ids")
                next_id = selected_next
                migrated = True
        return list(records), CatalogState(
            tuple(sessions[key] for key in sorted(sessions)),
            active,
            next_id,
            expected_revision - 1,
            migrated,
        )

    def _read_session_locked(self, session_id: int) -> StoredSession:
        return self._read_session_file(session_id)[1]

    def _read_session_file(
        self, session_id: int
    ) -> tuple[list[dict[str, Any]], StoredSession]:
        path = self._session_path(session_id)
        if not path.exists():
            raise SessionStoreError(f"session file does not exist: {session_id}")
        records = self._read_records(path)
        return records, self._parse_session_records(records, session_id)[1]

    def _parse_session_records(
        self,
        records: Sequence[dict[str, Any]],
        expected_session_id: int,
    ) -> tuple[list[dict[str, Any]], StoredSession]:
        if not records:
            raise SessionStoreError("session JSONL is empty")
        header = records[0]
        _exact_fields(
            header,
            {"version", "event", "id", "created_at", "metadata", "revision"},
            "session header",
        )
        _version(header["version"], "session header")
        if header["event"] != "session_header":
            raise SessionStoreError("session first event must be session_header")
        session_id = _positive_int(header["id"], "session id")
        if session_id != expected_session_id:
            raise SessionStoreError("session header id does not match its file")
        created_at = _timestamp_value(header["created_at"], "session created_at")
        if type(header["revision"]) is not int or header["revision"] != 0:
            raise SessionStoreError("session header revision must be zero")
        metadata = SessionMetadata.from_value(header["metadata"])
        messages: list[dict[str, str]] = []
        operations: list[TimelineOperation] = []
        expected_revision = 1
        for index, event in enumerate(records[1:], 2):
            kind = event.get("event") if isinstance(event, dict) else None
            common = {"version", "event", "timestamp", "revision", "session_id"}
            if kind == "metadata_changed":
                _exact_fields(event, common | {"metadata"}, f"session line {index}")
            elif kind == "turn_committed":
                _exact_fields(event, common | {"operations", "messages"}, f"session line {index}")
            else:
                raise SessionStoreError(f"session line {index} has an unknown event")
            _version(event["version"], f"session line {index}")
            _timestamp_value(event["timestamp"], f"session line {index} timestamp")
            if type(event["session_id"]) is not int or event["session_id"] != session_id:
                raise SessionStoreError(f"session id mismatch at line {index}")
            if type(event["revision"]) is not int or event["revision"] != expected_revision:
                raise SessionStoreError(f"session revision is not consecutive at line {index}")
            expected_revision += 1
            if kind == "metadata_changed":
                metadata = SessionMetadata.from_value(event["metadata"])
            else:
                raw_operations = event["operations"]
                if not isinstance(raw_operations, list) or not raw_operations:
                    raise SessionStoreError(f"session line {index} operations must be non-empty")
                operations.extend(
                    _operation_from_json(item, f"session line {index} operation")
                    for item in raw_operations
                )
                messages.extend(_messages_json(event["messages"]))
        try:
            timeline = SessionTimeline.from_operations(tuple(operations), acknowledge=True)
        except (TypeError, ValueError) as exc:
            raise SessionStoreError(f"session timeline replay failed: {exc}") from None
        return list(records), StoredSession(
            session_id,
            created_at,
            metadata,
            tuple(deepcopy(messages)),
            timeline,
            expected_revision - 1,
        )

    def _read_records(self, path: Path) -> list[dict[str, Any]]:
        try:
            data = path.read_bytes()
            text = data.decode("utf-8", errors="strict")
        except (OSError, UnicodeError) as exc:
            raise SessionStoreError(f"unable to read {path.name}: {exc}") from None
        if not text or not text.endswith("\n"):
            raise SessionStoreError(f"{path.name} must be newline-terminated JSONL")
        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line:
                raise SessionStoreError(f"{path.name} contains a blank line at {line_number}")
            try:
                value = json.loads(line, parse_constant=_reject_json_constant)
            except json.JSONDecodeError as exc:
                raise SessionStoreError(
                    f"{path.name} contains invalid JSON at line {line_number}: {exc.msg}"
                ) from None
            except ValueError as exc:
                raise SessionStoreError(
                    f"{path.name} contains invalid JSON at line {line_number}: {exc}"
                ) from None
            if not isinstance(value, dict):
                raise SessionStoreError(f"{path.name} line {line_number} must be an object")
            records.append(value)
        return records

    def _records_bytes(
        self, records: Sequence[Mapping[str, object]], secrets: Sequence[str]
    ) -> bytes:
        lines: list[str] = []
        for record in records:
            normalized = _strict_json(record, "event")
            try:
                lines.append(json.dumps(
                    normalized,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ))
            except (TypeError, ValueError) as exc:
                raise SessionStoreError(f"unable to serialize JSONL event: {exc}") from None
        serialized = "\n".join(lines) + "\n"
        for secret in secrets:
            if secret and secret in serialized:
                raise SessionStoreError("session data contains a sensitive value; write refused")
        return serialized.encode("utf-8")

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        temporary: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._write_temporary(path, payload)
            os.replace(temporary, path)
        except OSError as exc:
            raise SessionStoreError(f"unable to write {path.name}: {exc}") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _write_temporary(path: Path, payload: bytes) -> Path:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return temporary

    @staticmethod
    def _append_bytes(path: Path, payload: bytes) -> None:
        original_size: int | None = None
        try:
            with path.open("ab") as stream:
                stream.seek(0, os.SEEK_END)
                original_size = stream.tell()
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            if original_size is not None:
                try:
                    with path.open("r+b") as stream:
                        stream.truncate(original_size)
                        stream.flush()
                        os.fsync(stream.fileno())
                except OSError:
                    pass
            raise SessionStoreError(f"unable to append {path.name}: {exc}") from None

    def _timestamp(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime):
            raise SessionStoreError("clock must return a datetime")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _secrets(self, values: Sequence[str]) -> tuple[str, ...]:
        return (*self._sensitive_values, *(v for v in values if isinstance(v, str) and v))

    def _session_path(self, session_id: int) -> Path:
        return self.root / f"{session_id}.jsonl"

    @staticmethod
    def _catalog_header(timestamp: str) -> dict[str, object]:
        return {
            "version": SCHEMA_VERSION,
            "event": "catalog_header",
            "timestamp": timestamp,
            "revision": 0,
            "next_session_id": 1,
            "active_session_id": None,
        }

    @staticmethod
    def _session_header(
        session_id: int, created_at: str, metadata: SessionMetadata
    ) -> dict[str, object]:
        return {
            "version": SCHEMA_VERSION,
            "event": "session_header",
            "id": session_id,
            "created_at": created_at,
            "metadata": metadata.to_json(),
            "revision": 0,
        }


def _strict_json(value: object, location: str) -> Any:
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        try:
            value = dumper(mode="json")
        except Exception as exc:
            raise SessionStoreError(f"{location} model_dump failed: {exc}") from None
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SessionStoreError(f"{location} contains a non-finite number")
        return value
    if isinstance(value, (list, tuple)):
        return [_strict_json(item, f"{location}[]") for item in value]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SessionStoreError(f"{location} contains a non-string object key")
            result[key] = _strict_json(item, f"{location}.{key}")
        return result
    raise SessionStoreError(f"{location} contains an unsupported JSON type")


def _operation_json(operation: object) -> dict[str, object]:
    if not isinstance(operation, TimelineOperation):
        raise SessionStoreError("pending operations must be TimelineOperation values")
    return {
        "operation_id": operation.operation_id,
        "kind": operation.kind,
        "items": _strict_json(operation.items, "timeline operation items"),
        "block_ids": list(operation.block_ids),
        "turn_id": operation.turn_id,
    }


def _operation_from_json(value: object, location: str) -> TimelineOperation:
    if not isinstance(value, dict):
        raise SessionStoreError(f"{location} must be an object")
    _exact_fields(value, {"operation_id", "kind", "items", "block_ids", "turn_id"}, location)
    operation_id = _positive_int(value["operation_id"], f"{location}.operation_id")
    kind = value["kind"]
    items = value["items"]
    block_ids = value["block_ids"]
    turn_id = value["turn_id"]
    if not isinstance(kind, str):
        raise SessionStoreError(f"{location}.kind must be a string")
    if not isinstance(items, list):
        raise SessionStoreError(f"{location}.items must be an array")
    if not isinstance(block_ids, list):
        raise SessionStoreError(f"{location}.block_ids must be an array")
    selected_block_ids = tuple(
        _positive_int(block_id, f"{location}.block_ids") for block_id in block_ids
    )
    if turn_id is not None:
        turn_id = _positive_int(turn_id, f"{location}.turn_id")
    return TimelineOperation(
        operation_id,
        kind,
        tuple(deepcopy(items)),
        selected_block_ids,
        turn_id,
    )


def _messages_json(messages: object) -> list[dict[str, str]]:
    if isinstance(messages, (str, bytes)) or not isinstance(messages, Sequence):
        raise SessionStoreError("messages must be an array")
    result: list[dict[str, str]] = []
    for index, message in enumerate(messages):
        if (
            not isinstance(message, Mapping)
            or set(message) != {"speaker", "text"}
            or not isinstance(message["speaker"], str)
            or not isinstance(message["text"], str)
        ):
            raise SessionStoreError(
                f"messages[{index}] must contain only speaker/text strings"
            )
        result.append({"speaker": message["speaker"], "text": message["text"]})
    return result


def _exact_fields(value: object, fields: set[str], location: str) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise SessionStoreError(f"{location} fields are incomplete or unknown")


def _version(value: object, location: str) -> None:
    if type(value) is not int or value != SCHEMA_VERSION:
        raise SessionStoreError(f"{location} has unsupported version {value!r}")


def _positive_int(value: object, location: str) -> int:
    if type(value) is not int or value <= 0:
        raise SessionStoreError(f"{location} must be a positive integer")
    return value


def _nonnegative_int(value: object, location: str) -> int:
    if type(value) is not int or value < 0:
        raise SessionStoreError(f"{location} must be a non-negative integer")
    return value


def _timestamp_value(value: object, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise SessionStoreError(f"{location} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SessionStoreError(f"{location} must be an ISO timestamp") from None
    if parsed.tzinfo is None:
        raise SessionStoreError(f"{location} must include a timezone")
    return value


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-standard numeric constant {value}")
