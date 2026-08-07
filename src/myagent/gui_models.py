"""Persistent model registrations for the desktop GUI.

The JSON file deliberately lives outside the repository.  API keys are stored
as plain user configuration; callers must not display or log them.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit


MAX_MODEL_NAME_LENGTH = 120
MAX_API_KEY_LENGTH = 2048
MAX_BASE_URL_LENGTH = 2048


class ModelStoreError(RuntimeError):
    """A stable, user-presentable model store failure."""


class ModelValidationError(ModelStoreError):
    """A model registration failed validation."""


@dataclass(frozen=True)
class ModelRecord:
    """One registered model; the secret is excluded from repr comparisons."""

    name: str
    api_key: str = field(repr=False)
    created_at: str
    base_url: str | None = None

    @property
    def display(self) -> tuple[str, str]:
        return self.name, self.created_at


def default_model_store_path() -> Path:
    override = os.getenv("MYAGENT_GUI_HOME")
    if override:
        return Path(override).expanduser().resolve() / "models.json"
    appdata = os.getenv("APPDATA")
    if not appdata:
        appdata = str(Path.home() / "AppData" / "Roaming")
    return Path(appdata).expanduser().resolve() / "MyAgent" / "models.json"


class ModelStore:
    """Validate and atomically persist the GUI model registry."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else default_model_store_path()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def load(self) -> list[ModelRecord]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise ModelStoreError(f"无法读取模型配置：{exc}") from None
        except json.JSONDecodeError as exc:
            raise ModelStoreError(
                f"模型配置 JSON 已损坏（第 {exc.lineno} 行第 {exc.colno} 列）"
            ) from None
        if not isinstance(raw, dict) or not isinstance(raw.get("models"), list):
            raise ModelStoreError("模型配置格式无效：根对象必须包含 models 列表")
        records: list[ModelRecord] = []
        names: set[str] = set()
        for index, item in enumerate(raw["models"], start=1):
            if not isinstance(item, dict):
                raise ModelStoreError(f"模型配置格式无效：第 {index} 项不是对象")
            try:
                name = self._validate_name(item.get("name"))
                api_key = self._validate_key(item.get("api_key"))
                created_at = self._validate_created_at(item.get("created_at"))
                base_url = self._validate_base_url(item.get("base_url"))
            except ModelValidationError as exc:
                raise ModelStoreError(f"模型配置格式无效：第 {index} 项 {exc}") from None
            if name in names:
                raise ModelStoreError(f"模型配置格式无效：模型名称重复：{name}")
            names.add(name)
            records.append(ModelRecord(name, api_key, created_at, base_url))
        return records

    def add(
        self,
        name: object,
        api_key: object,
        base_url: object = None,
    ) -> ModelRecord:
        selected_name = self._validate_name(name)
        selected_key = self._validate_key(api_key)
        selected_base_url = self._validate_base_url(base_url)
        records = self.load()
        if any(record.name == selected_name for record in records):
            raise ModelValidationError(f"模型名称已存在：{selected_name}")
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        created_at = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        record = ModelRecord(
            selected_name,
            selected_key,
            created_at,
            selected_base_url,
        )
        self._write([*records, record])
        return record

    def delete(self, name: object) -> bool:
        selected_name = self._validate_name(name)
        records = self.load()
        kept = [record for record in records if record.name != selected_name]
        if len(kept) == len(records):
            return False
        self._write(kept)
        return True

    def get(self, name: str) -> ModelRecord | None:
        return next((record for record in self.load() if record.name == name), None)

    def _write(self, records: list[ModelRecord]) -> None:
        payload = {
            "models": [
                {
                    "name": record.name,
                    "api_key": record.api_key,
                    "base_url": record.base_url,
                    "created_at": record.created_at,
                }
                for record in records
            ]
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ModelStoreError(f"无法创建模型配置目录：{exc}") from None
        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            raise ModelStoreError(f"无法保存模型配置：{exc}") from None

    @staticmethod
    def _validate_name(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ModelValidationError("模型名不能为空")
        selected = value.strip()
        if len(selected) > MAX_MODEL_NAME_LENGTH:
            raise ModelValidationError(
                f"模型名不能超过 {MAX_MODEL_NAME_LENGTH} 个字符"
            )
        if any(character in selected for character in "\r\n\t"):
            raise ModelValidationError("模型名不能包含换行或制表符")
        return selected

    @staticmethod
    def _validate_key(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ModelValidationError("API key 不能为空")
        selected = value.strip()
        if len(selected) > MAX_API_KEY_LENGTH:
            raise ModelValidationError(
                f"API key 不能超过 {MAX_API_KEY_LENGTH} 个字符"
            )
        if any(character.isspace() for character in selected):
            raise ModelValidationError("API key 不能包含空白字符")
        return selected

    @staticmethod
    def _validate_base_url(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ModelValidationError("Base URL 必须是字符串")
        selected = value.strip()
        if not selected:
            return None
        if len(selected) > MAX_BASE_URL_LENGTH:
            raise ModelValidationError(
                f"Base URL 不能超过 {MAX_BASE_URL_LENGTH} 个字符"
            )
        if any(character.isspace() for character in selected):
            raise ModelValidationError("Base URL 不能包含空白字符")
        try:
            parsed = urlsplit(selected)
        except ValueError:
            raise ModelValidationError("Base URL 不是有效的 HTTP(S) 地址") from None
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ModelValidationError("Base URL 必须是完整的 HTTP(S) 地址")
        if parsed.username is not None or parsed.password is not None:
            raise ModelValidationError("Base URL 不能包含用户名或密码")
        if parsed.query or parsed.fragment:
            raise ModelValidationError("Base URL 不能包含查询参数或片段")
        return selected

    @staticmethod
    def _validate_created_at(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ModelValidationError("created_at 缺失")
        selected = value.strip()
        try:
            parsed = datetime.fromisoformat(selected.replace("Z", "+00:00"))
        except ValueError:
            raise ModelValidationError("created_at 不是有效的 ISO 时间") from None
        if parsed.tzinfo is None:
            raise ModelValidationError("created_at 必须包含时区")
        return selected
