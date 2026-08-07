"""Versioned, repository-external persistence for completed GUI conversations."""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from .gui_models import default_model_store_path


SCHEMA_VERSION = 1


class ConversationStoreError(RuntimeError):
    """A stable, user-presentable conversation store failure."""


def default_conversation_store_path() -> Path:
    return default_model_store_path().with_name("conversations.json")


def _json_value(value: object, location: str = "history") -> Any:
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        try:
            value = dumper(mode="json")
        except Exception as exc:
            raise ConversationStoreError(f"{location} 无法转换为 JSON：{exc}") from None
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConversationStoreError(f"{location} 不能包含非有限数字")
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item, f"{location}[]") for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ConversationStoreError(f"{location} 的对象键必须是字符串")
            result[key] = _json_value(item, f"{location}.{key}")
        return result
    raise ConversationStoreError(f"{location} 包含不支持的 JSON 类型")


class ConversationStore:
    """Strictly validate and atomically replace one conversation JSON file."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_conversation_store_path()

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise ConversationStoreError(f"无法读取对话记录：{exc}") from None
        except json.JSONDecodeError as exc:
            raise ConversationStoreError(
                f"对话记录 JSON 已损坏（第 {exc.lineno} 行第 {exc.colno} 列）"
            ) from None
        try:
            return self.validate(raw)
        except ConversationStoreError as exc:
            raise ConversationStoreError(f"对话记录格式无效：{exc}") from None

    @classmethod
    def validate(cls, raw: object) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ConversationStoreError("根数据必须是对象")
        expected = {"version", "activeSessionId", "nextSessionId", "sessions"}
        if set(raw) != expected:
            raise ConversationStoreError("根字段必须完整且不能包含未知字段")
        if (
            isinstance(raw["version"], bool)
            or not isinstance(raw["version"], int)
            or raw["version"] != SCHEMA_VERSION
        ):
            raise ConversationStoreError(f"不支持的版本：{raw['version']!r}")
        sessions = raw["sessions"]
        if not isinstance(sessions, list) or not sessions:
            raise ConversationStoreError("sessions 必须是非空数组")
        normalized_sessions: list[dict[str, Any]] = []
        ids: set[int] = set()
        fields = {
            "id", "title", "workspace", "mainModel", "subModel", "kind",
            "messages", "history",
        }
        for index, item in enumerate(sessions):
            where = f"sessions[{index}]"
            if not isinstance(item, dict) or set(item) != fields:
                raise ConversationStoreError(f"{where} 字段无效")
            session_id = item["id"]
            if isinstance(session_id, bool) or not isinstance(session_id, int) or session_id <= 0:
                raise ConversationStoreError(f"{where}.id 必须是正整数")
            if session_id in ids:
                raise ConversationStoreError("对话 id 不能重复")
            ids.add(session_id)
            title = item["title"]
            if not isinstance(title, str) or not title.strip() or len(title) > 80:
                raise ConversationStoreError(f"{where}.title 必须是 1 到 80 个字符")
            workspace = item["workspace"]
            if not isinstance(workspace, str) or not workspace or not Path(workspace).is_absolute():
                raise ConversationStoreError(f"{where}.workspace 必须是绝对路径字符串")
            for name in ("mainModel", "subModel"):
                value = item[name]
                if not isinstance(value, str) or len(value) > 120:
                    raise ConversationStoreError(f"{where}.{name} 必须是模型名称字符串")
            if item["kind"] not in {"main", "sub"}:
                raise ConversationStoreError(f"{where}.kind 无效")
            messages = item["messages"]
            if not isinstance(messages, list):
                raise ConversationStoreError(f"{where}.messages 必须是数组")
            normalized_messages: list[dict[str, str]] = []
            for message_index, message in enumerate(messages):
                if (
                    not isinstance(message, dict)
                    or set(message) != {"speaker", "text"}
                    or not isinstance(message["speaker"], str)
                    or not isinstance(message["text"], str)
                ):
                    raise ConversationStoreError(
                        f"{where}.messages[{message_index}] 必须包含 speaker/text 字符串"
                    )
                normalized_messages.append(dict(message))
            history = item["history"]
            if not isinstance(history, list):
                raise ConversationStoreError(f"{where}.history 必须是数组")
            normalized_sessions.append({
                "id": session_id,
                "title": title,
                "workspace": workspace,
                "mainModel": item["mainModel"],
                "subModel": item["subModel"],
                "kind": item["kind"],
                "messages": normalized_messages,
                "history": _json_value(history, f"{where}.history"),
            })
        active = raw["activeSessionId"]
        if isinstance(active, bool) or not isinstance(active, int) or active not in ids:
            raise ConversationStoreError("activeSessionId 必须指向现有对话")
        next_id = raw["nextSessionId"]
        if isinstance(next_id, bool) or not isinstance(next_id, int) or next_id <= max(ids):
            raise ConversationStoreError("nextSessionId 必须大于所有现有 id")
        return {
            "version": SCHEMA_VERSION,
            "activeSessionId": active,
            "nextSessionId": next_id,
            "sessions": normalized_sessions,
        }

    def save(self, payload: object, *, sensitive_values: tuple[str, ...] = ()) -> None:
        validated = self.validate(payload)
        try:
            serialized = json.dumps(
                validated, ensure_ascii=False, indent=2, allow_nan=False
            ) + "\n"
        except (TypeError, ValueError) as exc:
            raise ConversationStoreError(f"无法序列化对话记录：{exc}") from None
        for secret in sensitive_values:
            if secret and secret in serialized:
                raise ConversationStoreError("对话记录包含模型凭据，已拒绝保存")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConversationStoreError(f"无法创建对话记录目录：{exc}") from None
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            raise ConversationStoreError(f"无法保存对话记录：{exc}") from None
