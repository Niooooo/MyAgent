"""Workspace-persistent long-term memories with progressive disclosure."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from .tooling import FunctionTool


STORE_MEMORY_TOOL = "store_memory"
SEARCH_MEMORY_ENTRIES_TOOL = "search_memory_entries"
EXTRACT_MEMORY_TOOL = "extract_memory"
UPDATE_MEMORY_TOOL = "update_memory"
DELETE_MEMORY_TOOL = "delete_memory"
GET_MEMORY_ORGANIZATION_STATUS_TOOL = "get_memory_organization_status"
ORGANIZE_MEMORY_TOOL = "organize_memory"
LONG_TERM_MEMORY_TOOL_NAMES = frozenset(
    {
        STORE_MEMORY_TOOL,
        SEARCH_MEMORY_ENTRIES_TOOL,
        EXTRACT_MEMORY_TOOL,
        UPDATE_MEMORY_TOOL,
        DELETE_MEMORY_TOOL,
        GET_MEMORY_ORGANIZATION_STATUS_TOOL,
        ORGANIZE_MEMORY_TOOL,
    }
)

LONG_TERM_MEMORY_DIRECTORY = Path(".myagent", "memories")
LONG_TERM_MEMORY_SCHEMA_VERSION = 1
LONG_TERM_MEMORY_STATE_SCHEMA_VERSION = 1
LONG_TERM_MEMORY_TRANSACTION_SCHEMA_VERSION = 1

DEFAULT_MERGE_CHANGE_THRESHOLD = 5
DEFAULT_MERGE_MIN_INTERVAL_SECONDS = 3_600.0

MAX_MEMORY_ENTRIES = 1_000
MAX_MEMORY_TITLE_CHARS = 200
MAX_MEMORY_TITLE_BYTES = 800
MAX_MEMORY_SUMMARY_CHARS = 1_000
MAX_MEMORY_SUMMARY_BYTES = 4_000
MAX_MEMORY_CONTENT_CHARS = 100_000
MAX_MEMORY_CONTENT_BYTES = 400_000
MAX_MEMORY_TAGS = 20
MAX_MEMORY_TAG_CHARS = 64
MAX_MEMORY_TAG_BYTES = 256
MAX_MEMORY_CATALOG_BYTES = 5_000_000
MAX_MEMORY_ENTRY_FILE_BYTES = 450_000
DEFAULT_MEMORY_SEARCH_LIMIT = 10
MAX_MEMORY_SEARCH_LIMIT = 50
DEFAULT_EXTRACT_MEMORY_CHARS = 4_000
MAX_EXTRACT_MEMORY_CHARS = 16_000
MAX_ORGANIZATION_OPERATIONS = 100
MAX_MEMORY_STATE_BYTES = 64_000
MAX_TRANSACTION_MANIFEST_BYTES = 1_000_000
MAX_TRANSACTION_TARGETS = MAX_ORGANIZATION_OPERATIONS * 2 + 2

ORGANIZATION_UPDATE_REASONS = frozenset(
    {"merge_duplicate", "resolve_conflict", "refresh_outdated"}
)
ORGANIZATION_DELETION_REASONS = frozenset(
    {"duplicate", "conflicting", "outdated"}
)

_MEMORY_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_UTC_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
_METADATA_KEYS = frozenset(
    {"id", "title", "summary", "tags", "created_at", "updated_at"}
)
_CATALOG_KEYS = frozenset({"schema_version", "entries"})
_ENTRY_KEYS = frozenset({"schema_version", "content", *_METADATA_KEYS})
_STATE_KEYS = frozenset(
    {
        "schema_version",
        "pending_mutations",
        "pending_since",
        "last_organized_at",
    }
)
_ORGANIZATION_PLAN_KEYS = frozenset({"updates", "deletions"})
_ORGANIZATION_UPDATE_KEYS = frozenset(
    {"id", "title", "summary", "content", "tags", "reason"}
)
_ORGANIZATION_DELETION_KEYS = frozenset(
    {"id", "reason", "superseded_by_id"}
)
_TRANSACTION_KEYS = frozenset({"schema_version", "status", "files"})
_TRANSACTION_FILE_KEYS = frozenset(
    {
        "target",
        "old_file",
        "old_sha256",
        "new_file",
        "new_sha256",
    }
)
_TRANSACTION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_TRANSACTION_BLOB_PATTERN = re.compile(r"^(?:old|new)-\d{3}\.bin$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class MemoryMetadata:
    """The complete metadata surface allowed in the authoritative Catalog."""

    id: str
    title: str
    summary: str
    tags: tuple[str, ...]
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "tags": list(self.tags),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ExtractedMemory:
    """One bounded character segment plus its validated Catalog metadata."""

    metadata: MemoryMetadata
    content: str
    offset: int
    next_offset: int
    truncated: bool


@dataclass(frozen=True)
class MemoryUpdateResult:
    """A full-replacement update outcome without echoing the body."""

    metadata: MemoryMetadata
    updated: bool


@dataclass(frozen=True)
class MemoryOrganizationStatus:
    """Persistent mutation counters plus the configured AND threshold result."""

    pending_mutations: int
    pending_since: str | None
    last_organized_at: str | None
    merge_change_threshold: int
    merge_min_interval_seconds: float
    changes_ready: bool
    time_ready: bool
    organization_due: bool
    seconds_until_time_threshold: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "pending_mutations": self.pending_mutations,
            "pending_since": self.pending_since,
            "last_organized_at": self.last_organized_at,
            "merge_change_threshold": self.merge_change_threshold,
            "merge_min_interval_seconds": self.merge_min_interval_seconds,
            "changes_ready": self.changes_ready,
            "time_ready": self.time_ready,
            "organization_due": self.organization_due,
            "seconds_until_time_threshold": self.seconds_until_time_threshold,
        }


@dataclass(frozen=True)
class OrganizationDeletion:
    """One explicitly classified removal from an organization plan."""

    id: str
    reason: str
    superseded_by_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "reason": self.reason,
            "superseded_by_id": self.superseded_by_id,
        }


@dataclass(frozen=True)
class MemoryOrganizationResult:
    """A skipped, no-op, or completely committed organization outcome."""

    organized: bool
    reason: str
    updated: tuple[MemoryMetadata, ...]
    deleted: tuple[OrganizationDeletion, ...]
    status: MemoryOrganizationStatus


@dataclass(frozen=True)
class _OrganizationState:
    pending_mutations: int = 0
    pending_since: str | None = None
    last_organized_at: str | None = None


@dataclass(frozen=True)
class _OrganizationUpdate:
    id: str
    title: str
    summary: str
    content: str
    tags: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class _OrganizationPlan:
    updates: tuple[_OrganizationUpdate, ...]
    deletions: tuple[OrganizationDeletion, ...]


@dataclass(frozen=True)
class _TransactionFile:
    target: str
    old_file: str | None
    old_sha256: str | None
    new_file: str | None
    new_sha256: str | None


class LongTermMemoryStoreError(RuntimeError):
    """Expected validation or storage failure safe to return to the model."""

    def __init__(self, code: str, error: str) -> None:
        super().__init__(error)
        self.code = code
        self.error = error


class LongTermMemoryStore:
    """Persist metadata and bodies under one workspace-scoped memory root.

    One shared ``RLock`` serializes compound operations made through this instance,
    including calls from parent and child agents in the current process. A bounded
    write-ahead journal rolls prepared changes back and committed changes forward
    after a process interruption. It does not provide cross-process isolation or
    locking, and it does not claim power-loss durability across files.
    """

    def __init__(
        self,
        workspace_root: str | os.PathLike[str] | None = None,
        *,
        max_entries: int = MAX_MEMORY_ENTRIES,
        max_catalog_bytes: int = MAX_MEMORY_CATALOG_BYTES,
        max_entry_file_bytes: int = MAX_MEMORY_ENTRY_FILE_BYTES,
        merge_change_threshold: int = DEFAULT_MERGE_CHANGE_THRESHOLD,
        merge_min_interval_seconds: float = DEFAULT_MERGE_MIN_INTERVAL_SECONDS,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._require_positive_integer(max_entries, "max_entries")
        self._require_positive_integer(max_catalog_bytes, "max_catalog_bytes")
        self._require_positive_integer(max_entry_file_bytes, "max_entry_file_bytes")
        self._require_positive_integer(
            merge_change_threshold,
            "merge_change_threshold",
        )
        self._require_non_negative_number(
            merge_min_interval_seconds,
            "merge_min_interval_seconds",
        )
        if max_entry_file_bytes <= MAX_MEMORY_CONTENT_BYTES:
            raise ValueError(
                "max_entry_file_bytes must be greater than MAX_MEMORY_CONTENT_BYTES"
            )
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if id_factory is not None and not callable(id_factory):
            raise TypeError("id_factory must be callable")

        self.workspace_root = Path(workspace_root or Path.cwd()).resolve()
        self.root = self.workspace_root / LONG_TERM_MEMORY_DIRECTORY
        self.catalog_path = self.root / "catalog.json"
        self.state_path = self.root / "state.json"
        self.entries_root = self.root / "entries"
        self.transactions_root = self.root / ".transactions"
        self.max_entries = max_entries
        self.max_catalog_bytes = max_catalog_bytes
        self.max_entry_file_bytes = max_entry_file_bytes
        self.merge_change_threshold = merge_change_threshold
        self.merge_min_interval_seconds = float(merge_min_interval_seconds)
        self._clock = clock or _utc_now
        self._id_factory = id_factory or _random_memory_id
        self._lock = RLock()

    def store(
        self,
        title: object,
        summary: object,
        content: object,
        tags: object,
    ) -> MemoryMetadata:
        """Create one new entry and commit its Catalog metadata last."""
        with self._lock:
            try:
                validated_title = self._validate_title(title)
                validated_summary = self._validate_summary(summary)
                validated_content = self._validate_content(content)
                validated_tags = self._validate_tags(tags)
                self._recover_transactions()
                catalog = self._read_catalog()
                state = self._read_state()
                if len(catalog) >= self.max_entries:
                    raise LongTermMemoryStoreError(
                        "memory_limit_reached",
                        (
                            "Long-term memory limit of "
                            f"{self.max_entries} has been reached"
                        ),
                    )

                memory_id = self._new_memory_id(catalog)
                timestamp = self._timestamp()
                metadata = MemoryMetadata(
                    id=memory_id,
                    title=validated_title,
                    summary=validated_summary,
                    tags=validated_tags,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                entry_text = self._serialize_entry(metadata, validated_content)
                next_catalog = tuple(
                    sorted((*catalog, metadata), key=lambda item: item.id)
                )
                catalog_text = self._serialize_catalog(next_catalog)
                next_state = self._increment_state(state, timestamp)
                state_text = self._serialize_state(next_state)
                self._validate_proposal_sizes(entry_text, catalog_text)

                self._ensure_write_layout()
                target = self.entries_root / f"{memory_id}.json"
                if target.exists() or target.is_symlink():
                    raise self._storage_error()
                self._commit_files(
                    {
                        target: entry_text.encode("utf-8"),
                        self.catalog_path: catalog_text.encode("utf-8"),
                        self.state_path: state_text.encode("utf-8"),
                    }
                )
                return metadata
            except LongTermMemoryStoreError:
                raise
            except (OSError, RuntimeError) as exc:
                raise self._storage_error() from exc

    def update(
        self,
        id: object,
        title: object,
        summary: object,
        content: object,
        tags: object,
    ) -> MemoryUpdateResult:
        """Fully replace one existing entry while preserving identity and creation."""
        memory_id = self._validate_id(id)
        validated_title = self._validate_title(title)
        validated_summary = self._validate_summary(summary)
        validated_content = self._validate_content(content)
        validated_tags = self._validate_tags(tags)
        with self._lock:
            try:
                self._recover_transactions()
                catalog = self._read_catalog()
                state = self._read_state()
                current = self._catalog_entry(catalog, memory_id)
                current_content = self._read_entry(current)
                if (
                    current.title == validated_title
                    and current.summary == validated_summary
                    and current.tags == validated_tags
                    and current_content == validated_content
                ):
                    return MemoryUpdateResult(current, False)

                timestamp = self._timestamp()
                self._require_monotonic_timestamp(timestamp, current.updated_at)
                updated = MemoryMetadata(
                    id=current.id,
                    title=validated_title,
                    summary=validated_summary,
                    tags=validated_tags,
                    created_at=current.created_at,
                    updated_at=timestamp,
                )
                next_catalog = tuple(
                    sorted(
                        (
                            updated if item.id == memory_id else item
                            for item in catalog
                        ),
                        key=lambda item: item.id,
                    )
                )
                entry_text = self._serialize_entry(updated, validated_content)
                catalog_text = self._serialize_catalog(next_catalog)
                next_state = self._increment_state(state, timestamp)
                state_text = self._serialize_state(next_state)
                self._validate_proposal_sizes(entry_text, catalog_text)
                self._ensure_write_layout()
                self._commit_files(
                    {
                        self.entries_root / f"{memory_id}.json": entry_text.encode(
                            "utf-8"
                        ),
                        self.catalog_path: catalog_text.encode("utf-8"),
                        self.state_path: state_text.encode("utf-8"),
                    }
                )
                return MemoryUpdateResult(updated, True)
            except LongTermMemoryStoreError:
                raise
            except (OSError, RuntimeError) as exc:
                raise self._storage_error() from exc

    def delete(self, id: object) -> MemoryMetadata:
        """Delete one valid Catalog entry without returning its complete body."""
        memory_id = self._validate_id(id)
        with self._lock:
            try:
                self._recover_transactions()
                catalog = self._read_catalog()
                state = self._read_state()
                current = self._catalog_entry(catalog, memory_id)
                self._read_entry(current)
                timestamp = self._timestamp()
                next_catalog = tuple(
                    item for item in catalog if item.id != memory_id
                )
                catalog_text = self._serialize_catalog(next_catalog)
                next_state = self._increment_state(state, timestamp)
                state_text = self._serialize_state(next_state)
                self._validate_catalog_size(catalog_text)
                self._ensure_write_layout()
                self._commit_files(
                    {
                        self.catalog_path: catalog_text.encode("utf-8"),
                        self.state_path: state_text.encode("utf-8"),
                        self.entries_root / f"{memory_id}.json": None,
                    }
                )
                return current
            except LongTermMemoryStoreError:
                raise
            except (OSError, RuntimeError) as exc:
                raise self._storage_error() from exc

    def search(
        self,
        query: object,
        limit: object = DEFAULT_MEMORY_SEARCH_LIMIT,
    ) -> tuple[MemoryMetadata, ...]:
        """Search only authoritative Catalog metadata with deterministic ordering."""
        normalized_query = self._validate_query(query)
        validated_limit = self._validate_search_limit(limit)
        with self._lock:
            try:
                self._recover_transactions()
                catalog = self._read_catalog()
            except LongTermMemoryStoreError:
                raise
            except (OSError, RuntimeError) as exc:
                raise self._storage_error() from exc

        folded_query = normalized_query.casefold()
        matches = [
            metadata
            for metadata in catalog
            if metadata.id == normalized_query
            or folded_query in metadata.title.casefold()
            or folded_query in metadata.summary.casefold()
            or any(folded_query in tag.casefold() for tag in metadata.tags)
        ]
        # Stable sorts are applied from the least to the most significant key.
        matches.sort(key=lambda metadata: metadata.id)
        matches.sort(
            key=lambda metadata: _parse_timestamp(metadata.updated_at),
            reverse=True,
        )
        matches.sort(key=lambda metadata: metadata.id != normalized_query)
        return tuple(matches[:validated_limit])

    def extract(
        self,
        id: object,
        offset: object = 0,
        max_chars: object = DEFAULT_EXTRACT_MEMORY_CHARS,
    ) -> ExtractedMemory:
        """Read one bounded body segment after Catalog and entry validation."""
        memory_id = self._validate_id(id)
        validated_offset = self._validate_offset(offset)
        validated_max_chars = self._validate_extract_max_chars(max_chars)
        with self._lock:
            try:
                self._recover_transactions()
                catalog = self._read_catalog()
                metadata = self._catalog_entry(catalog, memory_id)
                content = self._read_entry(metadata)
            except LongTermMemoryStoreError:
                raise
            except (OSError, RuntimeError) as exc:
                raise self._storage_error() from exc

        if validated_offset > len(content):
            raise LongTermMemoryStoreError(
                "invalid_memory_offset",
                "offset is beyond the end of the memory content",
            )
        selected = content[
            validated_offset : validated_offset + validated_max_chars
        ]
        next_offset = validated_offset + len(selected)
        return ExtractedMemory(
            metadata=metadata,
            content=selected,
            offset=validated_offset,
            next_offset=next_offset,
            truncated=next_offset < len(content),
        )

    def organization_status(self) -> MemoryOrganizationStatus:
        """Return the persistent mutation state without creating empty storage."""
        with self._lock:
            try:
                self._recover_transactions()
                state = self._read_state()
                return self._organization_status(state, self._now())
            except LongTermMemoryStoreError:
                raise
            except (OSError, RuntimeError) as exc:
                raise self._storage_error() from exc

    def organize(self, plan: object) -> MemoryOrganizationResult:
        """Validate and atomically commit one explicit semantic organization plan."""
        validated_plan = self._validate_organization_plan(plan)
        with self._lock:
            try:
                self._recover_transactions()
                catalog = self._read_catalog()
                state = self._read_state()
                now = self._now()
                status = self._organization_status(state, now)
                if not status.changes_ready:
                    return MemoryOrganizationResult(
                        False,
                        "change_threshold_not_met",
                        (),
                        (),
                        status,
                    )
                if not status.time_ready:
                    return MemoryOrganizationResult(
                        False,
                        "time_threshold_not_met",
                        (),
                        (),
                        status,
                    )

                metadata_by_id = {item.id: item for item in catalog}
                update_ids = {item.id for item in validated_plan.updates}
                deletion_ids = {item.id for item in validated_plan.deletions}
                referenced_ids = update_ids | deletion_ids | {
                    item.superseded_by_id
                    for item in validated_plan.deletions
                    if item.superseded_by_id is not None
                }
                if not referenced_ids.issubset(metadata_by_id):
                    raise self._invalid_organization_plan(
                        "Organization plan references an unknown memory id"
                    )
                for deletion in validated_plan.deletions:
                    replacement = deletion.superseded_by_id
                    if replacement is not None and replacement in deletion_ids:
                        raise self._invalid_organization_plan(
                            "A superseding memory cannot also be deleted"
                        )

                contents = {
                    memory_id: self._read_entry(metadata_by_id[memory_id])
                    for memory_id in referenced_ids
                }
                timestamp = _format_timestamp(now)
                next_by_id = dict(metadata_by_id)
                file_changes: dict[Path, bytes | None] = {}
                updated_items: list[MemoryMetadata] = []
                for proposal in validated_plan.updates:
                    current = metadata_by_id[proposal.id]
                    if (
                        current.title == proposal.title
                        and current.summary == proposal.summary
                        and current.tags == proposal.tags
                        and contents[proposal.id] == proposal.content
                    ):
                        continue
                    self._require_monotonic_timestamp(
                        timestamp,
                        current.updated_at,
                    )
                    updated = MemoryMetadata(
                        id=current.id,
                        title=proposal.title,
                        summary=proposal.summary,
                        tags=proposal.tags,
                        created_at=current.created_at,
                        updated_at=timestamp,
                    )
                    entry_text = self._serialize_entry(updated, proposal.content)
                    self._validate_entry_size(entry_text)
                    next_by_id[proposal.id] = updated
                    updated_items.append(updated)
                    file_changes[
                        self.entries_root / f"{proposal.id}.json"
                    ] = entry_text.encode("utf-8")

                for deletion in validated_plan.deletions:
                    next_by_id.pop(deletion.id)
                    file_changes[
                        self.entries_root / f"{deletion.id}.json"
                    ] = None

                if not updated_items and not validated_plan.deletions:
                    return MemoryOrganizationResult(
                        False,
                        "no_changes",
                        (),
                        (),
                        status,
                    )

                next_catalog = tuple(
                    sorted(next_by_id.values(), key=lambda item: item.id)
                )
                catalog_text = self._serialize_catalog(next_catalog)
                self._validate_catalog_size(catalog_text)
                reset_state = _OrganizationState(
                    pending_mutations=0,
                    pending_since=None,
                    last_organized_at=timestamp,
                )
                state_text = self._serialize_state(reset_state)
                file_changes[self.catalog_path] = catalog_text.encode("utf-8")
                file_changes[self.state_path] = state_text.encode("utf-8")
                self._ensure_write_layout()
                self._commit_files(file_changes)
                return MemoryOrganizationResult(
                    True,
                    "organized",
                    tuple(updated_items),
                    validated_plan.deletions,
                    self._organization_status(reset_state, now),
                )
            except LongTermMemoryStoreError:
                raise
            except (OSError, RuntimeError) as exc:
                raise self._storage_error() from exc

    def _read_catalog(self) -> tuple[MemoryMetadata, ...]:
        resolved_root = self._resolved_directory(self.root)
        if resolved_root is None:
            return ()
        if self.catalog_path.is_symlink():
            raise self._catalog_error()
        if not self.catalog_path.exists():
            return ()
        try:
            resolved_catalog = self.catalog_path.resolve(strict=True)
        except OSError as exc:
            raise self._catalog_error() from exc
        if (
            not self._is_within(resolved_catalog, resolved_root)
            or not resolved_catalog.is_file()
        ):
            raise self._catalog_error()
        try:
            if resolved_catalog.stat().st_size > self.max_catalog_bytes:
                raise self._catalog_error()
            data = resolved_catalog.read_bytes()
        except LongTermMemoryStoreError:
            raise
        except OSError as exc:
            raise self._storage_error() from exc
        if len(data) > self.max_catalog_bytes:
            raise self._catalog_error()
        try:
            document = _strict_json_loads(data.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise self._catalog_error() from exc
        if not isinstance(document, dict) or frozenset(document) != _CATALOG_KEYS:
            raise self._catalog_error()
        schema_version = document.get("schema_version")
        if schema_version != LONG_TERM_MEMORY_SCHEMA_VERSION or isinstance(
            schema_version,
            bool,
        ):
            raise self._catalog_error()
        entries = document.get("entries")
        if not isinstance(entries, list) or len(entries) > self.max_entries:
            raise self._catalog_error()

        metadata_items: list[MemoryMetadata] = []
        try:
            for entry in entries:
                metadata_items.append(self._metadata_from_mapping(entry))
        except LongTermMemoryStoreError as exc:
            raise self._catalog_error() from exc
        ids = [metadata.id for metadata in metadata_items]
        if len(ids) != len(set(ids)):
            raise self._catalog_error()
        return tuple(metadata_items)

    def _read_state(self) -> _OrganizationState:
        resolved_root = self._resolved_directory(self.root)
        if resolved_root is None:
            return _OrganizationState()
        if self.state_path.is_symlink():
            raise self._state_error()
        if not self.state_path.exists():
            return _OrganizationState()
        try:
            resolved_state = self.state_path.resolve(strict=True)
        except OSError as exc:
            raise self._state_error() from exc
        if (
            not self._is_within(resolved_state, resolved_root)
            or not resolved_state.is_file()
        ):
            raise self._state_error()
        try:
            if resolved_state.stat().st_size > MAX_MEMORY_STATE_BYTES:
                raise self._state_error()
            data = resolved_state.read_bytes()
        except LongTermMemoryStoreError:
            raise
        except OSError as exc:
            raise self._storage_error() from exc
        if len(data) > MAX_MEMORY_STATE_BYTES:
            raise self._state_error()
        try:
            document = _strict_json_loads(data.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise self._state_error() from exc
        if not isinstance(document, dict) or frozenset(document) != _STATE_KEYS:
            raise self._state_error()
        schema_version = document.get("schema_version")
        if schema_version != LONG_TERM_MEMORY_STATE_SCHEMA_VERSION or isinstance(
            schema_version,
            bool,
        ):
            raise self._state_error()
        pending_mutations = document.get("pending_mutations")
        if (
            not isinstance(pending_mutations, int)
            or isinstance(pending_mutations, bool)
            or pending_mutations < 0
        ):
            raise self._state_error()
        pending_since = self._optional_timestamp(document.get("pending_since"))
        last_organized_at = self._optional_timestamp(
            document.get("last_organized_at")
        )
        if (pending_mutations == 0) != (pending_since is None):
            raise self._state_error()
        if (
            pending_since is not None
            and last_organized_at is not None
            and _parse_timestamp(pending_since)
            < _parse_timestamp(last_organized_at)
        ):
            raise self._state_error()
        return _OrganizationState(
            pending_mutations=pending_mutations,
            pending_since=pending_since,
            last_organized_at=last_organized_at,
        )

    @staticmethod
    def _catalog_entry(
        catalog: Sequence[MemoryMetadata],
        memory_id: str,
    ) -> MemoryMetadata:
        entry = next((item for item in catalog if item.id == memory_id), None)
        if entry is None:
            raise LongTermMemoryStoreError(
                "memory_not_found",
                "Long-term memory entry does not exist in the Catalog",
            )
        return entry

    def _validate_organization_plan(self, value: object) -> _OrganizationPlan:
        try:
            if not isinstance(value, Mapping) or frozenset(value) != _ORGANIZATION_PLAN_KEYS:
                raise self._invalid_organization_plan(
                    "Organization plan must contain updates and deletions"
                )
            raw_updates = value.get("updates")
            raw_deletions = value.get("deletions")
            if not isinstance(raw_updates, list) or not isinstance(
                raw_deletions,
                list,
            ):
                raise self._invalid_organization_plan(
                    "Organization plan updates and deletions must be arrays"
                )
            if len(raw_updates) + len(raw_deletions) > MAX_ORGANIZATION_OPERATIONS:
                raise self._invalid_organization_plan(
                    "Organization plan exceeds the operation limit"
                )

            updates = tuple(
                self._organization_update_from_mapping(item)
                for item in raw_updates
            )
            deletions = tuple(
                self._organization_deletion_from_mapping(item)
                for item in raw_deletions
            )
            update_ids = [item.id for item in updates]
            deletion_ids = [item.id for item in deletions]
            if len(update_ids) != len(set(update_ids)) or len(deletion_ids) != len(
                set(deletion_ids)
            ):
                raise self._invalid_organization_plan(
                    "Organization plan contains duplicate operation ids"
                )
            if set(update_ids).intersection(deletion_ids):
                raise self._invalid_organization_plan(
                    "One memory cannot be updated and deleted in the same plan"
                )
            return _OrganizationPlan(updates, deletions)
        except LongTermMemoryStoreError as exc:
            if exc.code == "invalid_organization_plan":
                raise
            raise self._invalid_organization_plan(
                "Organization plan contains invalid data"
            ) from exc

    def _organization_update_from_mapping(
        self,
        value: object,
    ) -> _OrganizationUpdate:
        if not isinstance(value, Mapping) or frozenset(value) != _ORGANIZATION_UPDATE_KEYS:
            raise self._invalid_organization_plan(
                "Organization update does not use the expected schema"
            )
        reason = value.get("reason")
        if reason not in ORGANIZATION_UPDATE_REASONS:
            raise self._invalid_organization_plan(
                "Organization update reason is invalid"
            )
        return _OrganizationUpdate(
            id=self._validate_id(value.get("id")),
            title=self._validate_title(value.get("title")),
            summary=self._validate_summary(value.get("summary")),
            content=self._validate_content(value.get("content")),
            tags=self._validate_tags(value.get("tags")),
            reason=str(reason),
        )

    def _organization_deletion_from_mapping(
        self,
        value: object,
    ) -> OrganizationDeletion:
        if not isinstance(value, Mapping) or frozenset(value) != _ORGANIZATION_DELETION_KEYS:
            raise self._invalid_organization_plan(
                "Organization deletion does not use the expected schema"
            )
        reason = value.get("reason")
        if reason not in ORGANIZATION_DELETION_REASONS:
            raise self._invalid_organization_plan(
                "Organization deletion reason is invalid"
            )
        memory_id = self._validate_id(value.get("id"))
        raw_replacement = value.get("superseded_by_id")
        replacement = (
            None
            if raw_replacement is None
            else self._validate_id(raw_replacement)
        )
        if reason in {"duplicate", "conflicting"} and replacement is None:
            raise self._invalid_organization_plan(
                "Duplicate and conflicting deletions require a superseding id"
            )
        if replacement == memory_id:
            raise self._invalid_organization_plan(
                "A memory cannot supersede itself"
            )
        return OrganizationDeletion(memory_id, str(reason), replacement)

    def _organization_status(
        self,
        state: _OrganizationState,
        now: datetime,
    ) -> MemoryOrganizationStatus:
        for previous in (state.pending_since, state.last_organized_at):
            if previous is not None and now < _parse_timestamp(previous):
                raise LongTermMemoryStoreError(
                    "memory_clock_error",
                    "Long-term memory clock moved backwards",
                )
        changes_ready = state.pending_mutations >= self.merge_change_threshold
        if state.pending_since is None:
            elapsed = 0.0
            time_ready = False
        else:
            elapsed = (
                now - _parse_timestamp(state.pending_since)
            ).total_seconds()
            time_ready = elapsed >= self.merge_min_interval_seconds
        seconds_remaining = max(
            0.0,
            self.merge_min_interval_seconds - elapsed,
        )
        return MemoryOrganizationStatus(
            pending_mutations=state.pending_mutations,
            pending_since=state.pending_since,
            last_organized_at=state.last_organized_at,
            merge_change_threshold=self.merge_change_threshold,
            merge_min_interval_seconds=self.merge_min_interval_seconds,
            changes_ready=changes_ready,
            time_ready=time_ready,
            organization_due=changes_ready and time_ready,
            seconds_until_time_threshold=seconds_remaining,
        )

    def _increment_state(
        self,
        state: _OrganizationState,
        timestamp: str,
    ) -> _OrganizationState:
        for previous in (state.pending_since, state.last_organized_at):
            if previous is not None:
                self._require_monotonic_timestamp(timestamp, previous)
        return _OrganizationState(
            pending_mutations=state.pending_mutations + 1,
            pending_since=state.pending_since or timestamp,
            last_organized_at=state.last_organized_at,
        )

    @staticmethod
    def _serialize_state(state: _OrganizationState) -> str:
        return _serialize_json(
            {
                "schema_version": LONG_TERM_MEMORY_STATE_SCHEMA_VERSION,
                "pending_mutations": state.pending_mutations,
                "pending_since": state.pending_since,
                "last_organized_at": state.last_organized_at,
            }
        )

    def _read_entry(self, expected: MemoryMetadata) -> str:
        resolved_entries = self._resolved_directory(self.entries_root)
        if resolved_entries is None:
            raise LongTermMemoryStoreError(
                "memory_entry_missing",
                "Catalog entry has no corresponding memory file",
            )
        target = self.entries_root / f"{expected.id}.json"
        if target.is_symlink():
            raise LongTermMemoryStoreError(
                "memory_entry_corrupt",
                "Long-term memory entry is not a regular stored file",
            )
        if not target.exists():
            raise LongTermMemoryStoreError(
                "memory_entry_missing",
                "Catalog entry has no corresponding memory file",
            )
        try:
            resolved_target = target.resolve(strict=True)
        except OSError as exc:
            raise LongTermMemoryStoreError(
                "memory_entry_corrupt",
                "Long-term memory entry cannot be resolved safely",
            ) from exc
        if (
            not self._is_within(resolved_target, resolved_entries)
            or not resolved_target.is_file()
        ):
            raise LongTermMemoryStoreError(
                "memory_entry_corrupt",
                "Long-term memory entry is not a regular stored file",
            )
        try:
            if resolved_target.stat().st_size > self.max_entry_file_bytes:
                raise LongTermMemoryStoreError(
                    "memory_entry_corrupt",
                    "Long-term memory entry exceeds the configured file limit",
                )
            data = resolved_target.read_bytes()
        except LongTermMemoryStoreError:
            raise
        except OSError as exc:
            raise self._storage_error() from exc
        if len(data) > self.max_entry_file_bytes:
            raise LongTermMemoryStoreError(
                "memory_entry_corrupt",
                "Long-term memory entry exceeds the configured file limit",
            )
        try:
            document = _strict_json_loads(data.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise LongTermMemoryStoreError(
                "memory_entry_corrupt",
                "Long-term memory entry is not valid UTF-8 JSON",
            ) from exc
        if not isinstance(document, dict) or frozenset(document) != _ENTRY_KEYS:
            raise LongTermMemoryStoreError(
                "memory_entry_corrupt",
                "Long-term memory entry does not use the expected schema",
            )
        schema_version = document.get("schema_version")
        if schema_version != LONG_TERM_MEMORY_SCHEMA_VERSION or isinstance(
            schema_version,
            bool,
        ):
            raise LongTermMemoryStoreError(
                "memory_entry_corrupt",
                "Long-term memory entry uses an unsupported schema version",
            )
        try:
            actual = self._metadata_from_mapping(
                {key: document[key] for key in _METADATA_KEYS}
            )
            content = self._validate_content(document.get("content"))
        except LongTermMemoryStoreError as exc:
            raise LongTermMemoryStoreError(
                "memory_entry_corrupt",
                "Long-term memory entry contains invalid data",
            ) from exc
        if actual != expected:
            raise LongTermMemoryStoreError(
                "memory_metadata_mismatch",
                "Catalog metadata does not match the memory entry",
            )
        return content

    def _metadata_from_mapping(self, value: object) -> MemoryMetadata:
        if not isinstance(value, Mapping) or frozenset(value) != _METADATA_KEYS:
            raise LongTermMemoryStoreError(
                "invalid_memory_metadata",
                "Long-term memory metadata does not use the expected schema",
            )
        memory_id = self._validate_id(value.get("id"))
        title = self._validate_title(value.get("title"))
        summary = self._validate_summary(value.get("summary"))
        tags = self._validate_tags(value.get("tags"))
        created_at = self._validate_timestamp(value.get("created_at"))
        updated_at = self._validate_timestamp(value.get("updated_at"))
        if _parse_timestamp(updated_at) < _parse_timestamp(created_at):
            raise LongTermMemoryStoreError(
                "invalid_memory_metadata",
                "updated_at must not be earlier than created_at",
            )
        return MemoryMetadata(
            id=memory_id,
            title=title,
            summary=summary,
            tags=tags,
            created_at=created_at,
            updated_at=updated_at,
        )

    def _new_memory_id(self, catalog: Sequence[MemoryMetadata]) -> str:
        known_ids = {metadata.id for metadata in catalog}
        for _ in range(64):
            candidate = self._id_factory()
            if (
                not isinstance(candidate, str)
                or _MEMORY_ID_PATTERN.fullmatch(candidate) is None
            ):
                continue
            target = self.entries_root / f"{candidate}.json"
            if (
                candidate not in known_ids
                and not target.exists()
                and not target.is_symlink()
            ):
                return candidate
        raise LongTermMemoryStoreError(
            "memory_id_allocation_failed",
            "Could not allocate a unique long-term memory identifier",
        )

    def _timestamp(self) -> str:
        return _format_timestamp(self._now())

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise LongTermMemoryStoreError(
                "memory_clock_error",
                "Long-term memory clock must return a timezone-aware datetime",
            )
        return value.astimezone(timezone.utc)

    def _serialize_catalog(self, catalog: Sequence[MemoryMetadata]) -> str:
        return _serialize_json(
            {
                "schema_version": LONG_TERM_MEMORY_SCHEMA_VERSION,
                "entries": [metadata.as_dict() for metadata in catalog],
            }
        )

    @staticmethod
    def _serialize_entry(metadata: MemoryMetadata, content: str) -> str:
        return _serialize_json(
            {
                "schema_version": LONG_TERM_MEMORY_SCHEMA_VERSION,
                **metadata.as_dict(),
                "content": content,
            }
        )

    def _validate_proposal_sizes(self, entry_text: str, catalog_text: str) -> None:
        self._validate_entry_size(entry_text)
        self._validate_catalog_size(catalog_text)

    def _validate_entry_size(self, entry_text: str) -> None:
        if len(entry_text.encode("utf-8")) > self.max_entry_file_bytes:
            raise LongTermMemoryStoreError(
                "memory_entry_too_large",
                "Long-term memory entry exceeds the configured file limit",
            )

    def _validate_catalog_size(self, catalog_text: str) -> None:
        if len(catalog_text.encode("utf-8")) > self.max_catalog_bytes:
            raise LongTermMemoryStoreError(
                "memory_catalog_too_large",
                "Long-term memory Catalog exceeds the configured file limit",
            )

    def _ensure_write_layout(self) -> None:
        current = self.workspace_root
        for part in (*LONG_TERM_MEMORY_DIRECTORY.parts, "entries"):
            candidate = current / part
            if candidate.is_symlink():
                raise LongTermMemoryStoreError(
                    "unsafe_memory_storage",
                    "Long-term memory storage cannot use symbolic-link directories",
                )
            if candidate.exists():
                if not candidate.is_dir():
                    raise LongTermMemoryStoreError(
                        "unsafe_memory_storage",
                        "Long-term memory storage location is not a directory",
                    )
                resolved = candidate.resolve(strict=True)
                if not self._is_within(resolved, self.workspace_root):
                    raise LongTermMemoryStoreError(
                        "unsafe_memory_storage",
                        "Long-term memory storage escapes the workspace",
                    )
            else:
                candidate.mkdir()
            current = candidate
        if self.catalog_path.is_symlink() or (
            self.catalog_path.exists() and not self.catalog_path.is_file()
        ):
            raise self._catalog_error()
        if self.state_path.is_symlink() or (
            self.state_path.exists() and not self.state_path.is_file()
        ):
            raise self._state_error()
        if self.transactions_root.is_symlink() or (
            self.transactions_root.exists()
            and not self.transactions_root.is_dir()
        ):
            raise LongTermMemoryStoreError(
                "unsafe_memory_storage",
                "Long-term memory transaction storage is unsafe",
            )

    def _commit_files(self, changes: Mapping[Path, bytes | None]) -> None:
        if not changes or len(changes) > MAX_TRANSACTION_TARGETS:
            raise self._storage_error()
        self._ensure_transaction_root()
        transaction_id = self._new_transaction_id()
        transaction_root = self.transactions_root / transaction_id
        transaction_root.mkdir()
        records: list[_TransactionFile] = []
        manifest_written = False
        try:
            ordered_changes = sorted(
                changes.items(),
                key=lambda item: self._target_relative(item[0]),
            )
            for index, (target, new_content) in enumerate(ordered_changes):
                relative_target = self._target_relative(target)
                old_content = self._read_target_bytes(target)
                old_file = None
                old_hash = None
                if old_content is not None:
                    old_file = f"old-{index:03d}.bin"
                    old_hash = _sha256(old_content)
                    self._write_stage_file(
                        transaction_root / old_file,
                        old_content,
                    )
                new_file = None
                new_hash = None
                if new_content is not None:
                    new_file = f"new-{index:03d}.bin"
                    new_hash = _sha256(new_content)
                    self._write_stage_file(
                        transaction_root / new_file,
                        new_content,
                    )
                records.append(
                    _TransactionFile(
                        target=relative_target,
                        old_file=old_file,
                        old_sha256=old_hash,
                        new_file=new_file,
                        new_sha256=new_hash,
                    )
                )

            self._write_transaction_manifest(
                transaction_root,
                "prepared",
                records,
            )
            manifest_written = True
            for target, new_content in ordered_changes:
                self._apply_target_bytes(target, new_content)
            self._write_transaction_manifest(
                transaction_root,
                "committed",
                records,
            )
        except BaseException:
            if manifest_written:
                try:
                    self._rollback_transaction(
                        transaction_root,
                        tuple(records),
                    )
                except BaseException:
                    pass
                else:
                    self._cleanup_transaction(transaction_root)
            else:
                self._cleanup_orphan_transaction(transaction_root)
            raise
        try:
            self._cleanup_transaction(transaction_root)
        except (OSError, LongTermMemoryStoreError):
            # A committed journal is safe to roll forward and clean next time.
            pass

    def _recover_transactions(self) -> None:
        resolved_root = self._resolved_directory(self.root)
        if resolved_root is None:
            return
        resolved_transactions = self._resolved_directory(self.transactions_root)
        if resolved_transactions is None:
            return
        if not self._is_within(resolved_transactions, resolved_root):
            raise self._transaction_error()
        for transaction_root in sorted(
            self.transactions_root.iterdir(),
            key=lambda path: path.name,
        ):
            if (
                transaction_root.is_symlink()
                or not transaction_root.is_dir()
                or _TRANSACTION_ID_PATTERN.fullmatch(transaction_root.name) is None
            ):
                raise self._transaction_error()
            manifest_path = transaction_root / "manifest.json"
            if not manifest_path.exists() and not manifest_path.is_symlink():
                self._cleanup_orphan_transaction(transaction_root)
                continue
            status, records = self._read_transaction_manifest(transaction_root)
            if status == "prepared":
                self._rollback_transaction(transaction_root, records)
            else:
                self._rollforward_transaction(transaction_root, records)
            self._cleanup_transaction(transaction_root)

    def _ensure_transaction_root(self) -> None:
        if self.transactions_root.is_symlink():
            raise self._transaction_error()
        if self.transactions_root.exists():
            if not self.transactions_root.is_dir():
                raise self._transaction_error()
        else:
            self.transactions_root.mkdir()
        resolved = self.transactions_root.resolve(strict=True)
        resolved_root = self.root.resolve(strict=True)
        if not self._is_within(resolved, resolved_root):
            raise self._transaction_error()

    def _new_transaction_id(self) -> str:
        for _ in range(64):
            candidate = secrets.token_hex(16)
            target = self.transactions_root / candidate
            if not target.exists() and not target.is_symlink():
                return candidate
        raise self._storage_error()

    def _write_transaction_manifest(
        self,
        transaction_root: Path,
        status: str,
        records: Sequence[_TransactionFile],
    ) -> None:
        document = {
            "schema_version": LONG_TERM_MEMORY_TRANSACTION_SCHEMA_VERSION,
            "status": status,
            "files": [
                {
                    "target": record.target,
                    "old_file": record.old_file,
                    "old_sha256": record.old_sha256,
                    "new_file": record.new_file,
                    "new_sha256": record.new_sha256,
                }
                for record in records
            ],
        }
        data = _serialize_json(document).encode("utf-8")
        if len(data) > MAX_TRANSACTION_MANIFEST_BYTES:
            raise self._storage_error()
        self._atomic_write_bytes(transaction_root / "manifest.json", data)

    def _read_transaction_manifest(
        self,
        transaction_root: Path,
    ) -> tuple[str, tuple[_TransactionFile, ...]]:
        manifest_path = transaction_root / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise self._transaction_error()
        data = manifest_path.read_bytes()
        if len(data) > MAX_TRANSACTION_MANIFEST_BYTES:
            raise self._transaction_error()
        try:
            document = _strict_json_loads(data.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise self._transaction_error() from exc
        if not isinstance(document, dict) or frozenset(document) != _TRANSACTION_KEYS:
            raise self._transaction_error()
        schema_version = document.get("schema_version")
        status = document.get("status")
        files = document.get("files")
        if (
            schema_version != LONG_TERM_MEMORY_TRANSACTION_SCHEMA_VERSION
            or isinstance(schema_version, bool)
            or status not in {"prepared", "committed"}
            or not isinstance(files, list)
            or not files
            or len(files) > MAX_TRANSACTION_TARGETS
        ):
            raise self._transaction_error()
        records = tuple(
            self._transaction_file_from_mapping(item) for item in files
        )
        targets = [record.target for record in records]
        stage_files = [
            file_name
            for record in records
            for file_name in (record.old_file, record.new_file)
            if file_name is not None
        ]
        if len(targets) != len(set(targets)) or len(stage_files) != len(
            set(stage_files)
        ):
            raise self._transaction_error()
        return str(status), records

    def _transaction_file_from_mapping(self, value: object) -> _TransactionFile:
        if not isinstance(value, Mapping) or frozenset(value) != _TRANSACTION_FILE_KEYS:
            raise self._transaction_error()
        target = value.get("target")
        if not isinstance(target, str):
            raise self._transaction_error()
        self._target_from_relative(target)
        old_file, old_hash = self._validated_transaction_version(
            value.get("old_file"),
            value.get("old_sha256"),
            "old",
        )
        new_file, new_hash = self._validated_transaction_version(
            value.get("new_file"),
            value.get("new_sha256"),
            "new",
        )
        return _TransactionFile(
            target,
            old_file,
            old_hash,
            new_file,
            new_hash,
        )

    def _validated_transaction_version(
        self,
        file_name: object,
        digest: object,
        expected_prefix: str,
    ) -> tuple[str | None, str | None]:
        if file_name is None and digest is None:
            return None, None
        if (
            not isinstance(file_name, str)
            or _TRANSACTION_BLOB_PATTERN.fullmatch(file_name) is None
            or not file_name.startswith(f"{expected_prefix}-")
            or not isinstance(digest, str)
            or _SHA256_PATTERN.fullmatch(digest) is None
        ):
            raise self._transaction_error()
        return file_name, digest

    def _rollback_transaction(
        self,
        transaction_root: Path,
        records: Sequence[_TransactionFile],
    ) -> None:
        for record in reversed(records):
            self._restore_transaction_version(
                transaction_root,
                record,
                use_old=True,
            )

    def _rollforward_transaction(
        self,
        transaction_root: Path,
        records: Sequence[_TransactionFile],
    ) -> None:
        for record in records:
            self._restore_transaction_version(
                transaction_root,
                record,
                use_old=False,
            )

    def _restore_transaction_version(
        self,
        transaction_root: Path,
        record: _TransactionFile,
        *,
        use_old: bool,
    ) -> None:
        target = self._target_from_relative(record.target)
        file_name = record.old_file if use_old else record.new_file
        digest = record.old_sha256 if use_old else record.new_sha256
        if file_name is None:
            self._apply_target_bytes(target, None)
            return
        stage_path = transaction_root / file_name
        if stage_path.is_symlink():
            raise self._transaction_error()
        if stage_path.exists():
            data = stage_path.read_bytes()
            if len(data) > self._target_size_limit(target) or _sha256(data) != digest:
                raise self._transaction_error()
            self._apply_target_bytes(target, data)
            return
        if not self._target_matches(target, digest):
            raise self._transaction_error()

    def _cleanup_transaction(self, transaction_root: Path) -> None:
        for entry in tuple(transaction_root.iterdir()):
            if entry.is_symlink() or not entry.is_file():
                raise self._transaction_error()
            if entry.name != "manifest.json" and (
                _TRANSACTION_BLOB_PATTERN.fullmatch(entry.name) is None
                and not entry.name.startswith(".manifest.json.")
            ):
                raise self._transaction_error()
            entry.unlink()
        transaction_root.rmdir()

    def _cleanup_orphan_transaction(self, transaction_root: Path) -> None:
        self._cleanup_transaction(transaction_root)

    def _target_relative(self, target: Path) -> str:
        try:
            relative = target.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise self._transaction_error() from exc
        self._target_from_relative(relative)
        return relative

    def _target_from_relative(self, relative: str) -> Path:
        if relative in {"catalog.json", "state.json"}:
            return self.root / relative
        parts = Path(relative).parts
        if (
            len(parts) == 2
            and parts[0] == "entries"
            and parts[1].endswith(".json")
            and _MEMORY_ID_PATTERN.fullmatch(parts[1][:-5]) is not None
        ):
            return self.root / parts[0] / parts[1]
        raise self._transaction_error()

    def _read_target_bytes(self, target: Path) -> bytes | None:
        if target.is_symlink():
            raise self._transaction_error()
        if not target.exists():
            return None
        if not target.is_file():
            raise self._transaction_error()
        data = target.read_bytes()
        if len(data) > self._target_size_limit(target):
            raise self._transaction_error()
        return data

    def _target_size_limit(self, target: Path) -> int:
        if target == self.catalog_path:
            return self.max_catalog_bytes
        if target == self.state_path:
            return MAX_MEMORY_STATE_BYTES
        return self.max_entry_file_bytes

    def _apply_target_bytes(self, target: Path, content: bytes | None) -> None:
        self._target_relative(target)
        if target.is_symlink():
            raise self._transaction_error()
        if content is None:
            if target.exists():
                if not target.is_file():
                    raise self._transaction_error()
                target.unlink()
            return
        if len(content) > self._target_size_limit(target):
            raise self._transaction_error()
        self._atomic_write_bytes(target, content)

    def _target_matches(self, target: Path, digest: str) -> bool:
        if target.is_symlink() or not target.is_file():
            return False
        data = target.read_bytes()
        return len(data) <= self._target_size_limit(target) and _sha256(data) == digest

    @staticmethod
    def _write_stage_file(target: Path, content: bytes) -> None:
        with target.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    def _resolved_directory(self, path: Path) -> Path | None:
        if path.is_symlink():
            raise LongTermMemoryStoreError(
                "unsafe_memory_storage",
                "Long-term memory storage cannot use symbolic-link directories",
            )
        if not path.exists():
            return None
        if not path.is_dir():
            raise LongTermMemoryStoreError(
                "unsafe_memory_storage",
                "Long-term memory storage location is not a directory",
            )
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise LongTermMemoryStoreError(
                "unsafe_memory_storage",
                "Long-term memory storage cannot be resolved safely",
            ) from exc
        if not self._is_within(resolved, self.workspace_root):
            raise LongTermMemoryStoreError(
                "unsafe_memory_storage",
                "Long-term memory storage escapes the workspace",
            )
        return resolved

    @staticmethod
    def _atomic_write(target: Path, content: str) -> None:
        LongTermMemoryStore._atomic_write_bytes(
            target,
            content.encode("utf-8"),
        )

    @staticmethod
    def _atomic_write_bytes(target: Path, content: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        descriptor_open = True
        try:
            with os.fdopen(
                descriptor,
                "wb",
            ) as stream:
                descriptor_open = False
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, target)
        except BaseException:
            if descriptor_open:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass
            raise

    @staticmethod
    def _validate_id(value: object) -> str:
        if not isinstance(value, str) or _MEMORY_ID_PATTERN.fullmatch(value) is None:
            raise LongTermMemoryStoreError(
                "invalid_memory_id",
                "id must be exactly 32 lowercase hexadecimal characters",
            )
        return value

    @staticmethod
    def _validate_query(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise LongTermMemoryStoreError(
                "invalid_memory_query",
                "query must be a non-empty string",
            )
        if "\x00" in value:
            raise LongTermMemoryStoreError(
                "invalid_memory_query",
                "query must be text without null characters",
            )
        return value.strip()

    @staticmethod
    def _validate_search_limit(value: object) -> int:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 1 <= value <= MAX_MEMORY_SEARCH_LIMIT
        ):
            raise LongTermMemoryStoreError(
                "invalid_memory_limit",
                f"limit must be an integer between 1 and {MAX_MEMORY_SEARCH_LIMIT}",
            )
        return value

    @staticmethod
    def _validate_offset(value: object) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise LongTermMemoryStoreError(
                "invalid_memory_offset",
                "offset must be a non-negative integer",
            )
        return value

    @staticmethod
    def _validate_extract_max_chars(value: object) -> int:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 1 <= value <= MAX_EXTRACT_MEMORY_CHARS
        ):
            raise LongTermMemoryStoreError(
                "invalid_memory_max_chars",
                (
                    "max_chars must be an integer between 1 and "
                    f"{MAX_EXTRACT_MEMORY_CHARS}"
                ),
            )
        return value

    def _validate_title(self, value: object) -> str:
        return self._validate_text(
            value,
            field="title",
            max_chars=MAX_MEMORY_TITLE_CHARS,
            max_bytes=MAX_MEMORY_TITLE_BYTES,
        )

    def _validate_summary(self, value: object) -> str:
        return self._validate_text(
            value,
            field="summary",
            max_chars=MAX_MEMORY_SUMMARY_CHARS,
            max_bytes=MAX_MEMORY_SUMMARY_BYTES,
        )

    def _validate_content(self, value: object) -> str:
        return self._validate_text(
            value,
            field="content",
            max_chars=MAX_MEMORY_CONTENT_CHARS,
            max_bytes=MAX_MEMORY_CONTENT_BYTES,
        )

    def _validate_tags(self, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise LongTermMemoryStoreError(
                "invalid_memory_tags",
                "tags must be an array of strings",
            )
        if len(value) > MAX_MEMORY_TAGS:
            raise LongTermMemoryStoreError(
                "memory_tag_limit_exceeded",
                f"tags exceed the configured limit of {MAX_MEMORY_TAGS}",
            )
        tags = []
        for tag in value:
            tags.append(
                self._validate_text(
                    tag,
                    field="tag",
                    max_chars=MAX_MEMORY_TAG_CHARS,
                    max_bytes=MAX_MEMORY_TAG_BYTES,
                )
            )
        return tuple(tags)

    @staticmethod
    def _validate_text(
        value: object,
        *,
        field: str,
        max_chars: int,
        max_bytes: int,
    ) -> str:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise LongTermMemoryStoreError(
                f"invalid_memory_{field}",
                f"{field} must be non-empty text without null characters",
            )
        if len(value) > max_chars:
            raise LongTermMemoryStoreError(
                f"memory_{field}_too_long",
                f"{field} exceeds the configured {max_chars}-character limit",
            )
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise LongTermMemoryStoreError(
                f"invalid_memory_{field}",
                f"{field} must be valid Unicode text",
            ) from exc
        if len(encoded) > max_bytes:
            raise LongTermMemoryStoreError(
                f"memory_{field}_too_large",
                f"{field} exceeds the configured UTF-8 byte limit",
            )
        return value

    @staticmethod
    def _validate_timestamp(value: object) -> str:
        if (
            not isinstance(value, str)
            or _UTC_TIMESTAMP_PATTERN.fullmatch(value) is None
        ):
            raise LongTermMemoryStoreError(
                "invalid_memory_timestamp",
                "timestamp must be a UTC RFC3339 value",
            )
        try:
            _parse_timestamp(value)
        except ValueError as exc:
            raise LongTermMemoryStoreError(
                "invalid_memory_timestamp",
                "timestamp must be a UTC RFC3339 value",
            ) from exc
        return value

    def _optional_timestamp(self, value: object) -> str | None:
        if value is None:
            return None
        try:
            return self._validate_timestamp(value)
        except LongTermMemoryStoreError as exc:
            raise self._state_error() from exc

    @staticmethod
    def _require_monotonic_timestamp(current: str, previous: str) -> None:
        if _parse_timestamp(current) < _parse_timestamp(previous):
            raise LongTermMemoryStoreError(
                "memory_clock_error",
                "Long-term memory clock moved backwards",
            )

    @staticmethod
    def _require_positive_integer(value: object, name: str) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{name} must be an integer")
        if value <= 0:
            raise ValueError(f"{name} must be positive")

    @staticmethod
    def _require_non_negative_number(value: object, name: str) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError(f"{name} must be a number")
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0:
            raise ValueError(f"{name} must be finite and non-negative")

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    @staticmethod
    def _catalog_error() -> LongTermMemoryStoreError:
        return LongTermMemoryStoreError(
            "memory_catalog_corrupt",
            "Long-term memory Catalog is invalid",
        )

    @staticmethod
    def _state_error() -> LongTermMemoryStoreError:
        return LongTermMemoryStoreError(
            "memory_organization_state_corrupt",
            "Long-term memory organization state is invalid",
        )

    @staticmethod
    def _transaction_error() -> LongTermMemoryStoreError:
        return LongTermMemoryStoreError(
            "memory_transaction_corrupt",
            "Long-term memory transaction journal is invalid",
        )

    @staticmethod
    def _invalid_organization_plan(error: str) -> LongTermMemoryStoreError:
        return LongTermMemoryStoreError(
            "invalid_organization_plan",
            error,
        )

    @staticmethod
    def _storage_error() -> LongTermMemoryStoreError:
        return LongTermMemoryStoreError(
            "memory_storage_error",
            "Long-term memory storage operation failed",
        )


def long_term_memory_tools(store: LongTermMemoryStore) -> list[FunctionTool]:
    """Build the complete long-term-memory tool family for one shared Store."""

    def store_memory(
        title: str,
        summary: str,
        content: str,
        tags: list[str],
    ) -> dict[str, Any]:
        try:
            metadata = store.store(title, summary, content, tags)
            status = store.organization_status()
        except LongTermMemoryStoreError as exc:
            return _error_result(exc)
        return {
            "ok": True,
            "created": True,
            **metadata.as_dict(),
            "organization": status.as_dict(),
        }

    def search_memory_entries(
        query: str,
        limit: int = DEFAULT_MEMORY_SEARCH_LIMIT,
    ) -> dict[str, Any]:
        try:
            entries = store.search(query, limit)
        except LongTermMemoryStoreError as exc:
            return _error_result(exc)
        return {
            "ok": True,
            "entries": [metadata.as_dict() for metadata in entries],
            "count": len(entries),
        }

    def extract_memory(
        id: str,
        offset: int = 0,
        max_chars: int = DEFAULT_EXTRACT_MEMORY_CHARS,
    ) -> dict[str, Any]:
        try:
            extracted = store.extract(id, offset, max_chars)
        except LongTermMemoryStoreError as exc:
            return _error_result(exc)
        return {
            "ok": True,
            **extracted.metadata.as_dict(),
            "content": extracted.content,
            "offset": extracted.offset,
            "next_offset": extracted.next_offset,
            "truncated": extracted.truncated,
        }

    def update_memory(
        id: str,
        title: str,
        summary: str,
        content: str,
        tags: list[str],
    ) -> dict[str, Any]:
        try:
            result = store.update(id, title, summary, content, tags)
            status = store.organization_status()
        except LongTermMemoryStoreError as exc:
            return _error_result(exc)
        return {
            "ok": True,
            "updated": result.updated,
            **result.metadata.as_dict(),
            "organization": status.as_dict(),
        }

    def delete_memory(id: str) -> dict[str, Any]:
        try:
            metadata = store.delete(id)
            status = store.organization_status()
        except LongTermMemoryStoreError as exc:
            return _error_result(exc)
        return {
            "ok": True,
            "deleted": True,
            **metadata.as_dict(),
            "organization": status.as_dict(),
        }

    def get_memory_organization_status() -> dict[str, Any]:
        try:
            status = store.organization_status()
        except LongTermMemoryStoreError as exc:
            return _error_result(exc)
        return {"ok": True, **status.as_dict()}

    def organize_memory(plan: dict[str, Any]) -> dict[str, Any]:
        try:
            result = store.organize(plan)
        except LongTermMemoryStoreError as exc:
            return _error_result(exc)
        return {
            "ok": True,
            "organized": result.organized,
            "reason": result.reason,
            "updated": [item.as_dict() for item in result.updated],
            "deleted": [item.as_dict() for item in result.deleted],
            "organization": result.status.as_dict(),
        }

    id_property = {
        "type": "string",
        "pattern": _MEMORY_ID_PATTERN.pattern,
        "minLength": 32,
        "maxLength": 32,
    }
    update_properties = {
        "id": id_property,
        "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_MEMORY_TITLE_CHARS,
        },
        "summary": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_MEMORY_SUMMARY_CHARS,
        },
        "content": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_MEMORY_CONTENT_CHARS,
        },
        "tags": {
            "type": "array",
            "maxItems": MAX_MEMORY_TAGS,
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_MEMORY_TAG_CHARS,
            },
        },
    }
    organization_update_properties = {
        **update_properties,
        "reason": {
            "type": "string",
            "enum": sorted(ORGANIZATION_UPDATE_REASONS),
        },
    }
    organization_deletion_properties = {
        "id": id_property,
        "reason": {
            "type": "string",
            "enum": sorted(ORGANIZATION_DELETION_REASONS),
        },
        "superseded_by_id": {
            "anyOf": [id_property, {"type": "null"}],
        },
    }

    return [
        FunctionTool(
            name=STORE_MEMORY_TOOL,
            description=(
                "Create a new workspace-persistent long-term memory. The identifier "
                "is generated by the Store; this never updates or overwrites an entry."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_MEMORY_TITLE_CHARS,
                    },
                    "summary": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_MEMORY_SUMMARY_CHARS,
                    },
                    "content": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_MEMORY_CONTENT_CHARS,
                    },
                    "tags": {
                        "type": "array",
                        "maxItems": MAX_MEMORY_TAGS,
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_MEMORY_TAG_CHARS,
                        },
                    },
                },
                "required": ["title", "summary", "content", "tags"],
                "additionalProperties": False,
            },
            handler=store_memory,
            strict=True,
        ),
        FunctionTool(
            name=SEARCH_MEMORY_ENTRIES_TOOL,
            description=(
                "Search only long-term memory Catalog metadata. Results contain id, "
                "title, summary, tags, and timestamps, never full memory content."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_MEMORY_SEARCH_LIMIT,
                        "default": DEFAULT_MEMORY_SEARCH_LIMIT,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=search_memory_entries,
            strict=True,
        ),
        FunctionTool(
            name=EXTRACT_MEMORY_TOOL,
            description=(
                "Read a bounded character segment from one exact memory id returned "
                "by search_memory_entries. This accepts an id, never a path."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "pattern": _MEMORY_ID_PATTERN.pattern,
                        "minLength": 32,
                        "maxLength": 32,
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                    },
                    "max_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_EXTRACT_MEMORY_CHARS,
                        "default": DEFAULT_EXTRACT_MEMORY_CHARS,
                    },
                },
                "required": ["id"],
                "additionalProperties": False,
            },
            handler=extract_memory,
            strict=True,
        ),
        FunctionTool(
            name=UPDATE_MEMORY_TOOL,
            description=(
                "Fully replace one exact long-term memory while preserving its id "
                "and creation time. A no-op does not increment organization state."
            ),
            parameters={
                "type": "object",
                "properties": update_properties,
                "required": ["id", "title", "summary", "content", "tags"],
                "additionalProperties": False,
            },
            handler=update_memory,
            strict=True,
        ),
        FunctionTool(
            name=DELETE_MEMORY_TOOL,
            description=(
                "Delete one exact long-term memory selected by id. This never "
                "returns the deleted body and requires fresh user approval."
            ),
            parameters={
                "type": "object",
                "properties": {"id": id_property},
                "required": ["id"],
                "additionalProperties": False,
            },
            handler=delete_memory,
            strict=True,
        ),
        FunctionTool(
            name=GET_MEMORY_ORGANIZATION_STATUS_TOOL,
            description=(
                "Read whether both the mutation-count and elapsed-time thresholds "
                "are satisfied before preparing an organization plan."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            handler=get_memory_organization_status,
            strict=True,
        ),
        FunctionTool(
            name=ORGANIZE_MEMORY_TOOL,
            description=(
                "Commit an explicit organization plan after search and extraction. "
                "The caller, not the Store, judges duplicates, conflicts, and "
                "outdated memories. Both configured thresholds must be satisfied."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "object",
                        "properties": {
                            "updates": {
                                "type": "array",
                                "maxItems": MAX_ORGANIZATION_OPERATIONS,
                                "items": {
                                    "type": "object",
                                    "properties": organization_update_properties,
                                    "required": [
                                        "id",
                                        "title",
                                        "summary",
                                        "content",
                                        "tags",
                                        "reason",
                                    ],
                                    "additionalProperties": False,
                                },
                            },
                            "deletions": {
                                "type": "array",
                                "maxItems": MAX_ORGANIZATION_OPERATIONS,
                                "items": {
                                    "type": "object",
                                    "properties": organization_deletion_properties,
                                    "required": [
                                        "id",
                                        "reason",
                                        "superseded_by_id",
                                    ],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["updates", "deletions"],
                        "additionalProperties": False,
                    }
                },
                "required": ["plan"],
                "additionalProperties": False,
            },
            handler=organize_memory,
            strict=True,
        ),
    ]


def _error_result(exc: LongTermMemoryStoreError) -> dict[str, Any]:
    return {"ok": False, "code": exc.code, "error": exc.error}


def _serialize_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _strict_json_loads(value: str) -> object:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = item
        return result

    def reject_constant(value: str) -> object:
        raise ValueError("non-finite JSON number")

    return json.loads(
        value,
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _random_memory_id() -> str:
    return secrets.token_hex(16)
