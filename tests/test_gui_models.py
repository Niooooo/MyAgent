import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from myagent.gui_models import (
    DesktopSettingsError,
    DesktopSettingsStore,
    MAX_API_KEY_LENGTH,
    MAX_BASE_URL_LENGTH,
    MAX_MODEL_NAME_LENGTH,
    ModelStore,
    ModelStoreError,
    ModelValidationError,
    default_model_store_path,
)


class DesktopSettingsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "settings.json"
        self.store = DesktopSettingsStore(self.path)

    def test_defaults_round_trip_and_strict_integer_validation(self) -> None:
        defaults = self.store.load()
        self.store.save(defaults)
        self.assertEqual(self.store.load(), defaults)
        invalid = defaults.public_dict()
        invalid["maxToolRounds"] = True
        with self.assertRaisesRegex(DesktopSettingsError, "必须是整数"):
            self.store.validate(invalid)

    def test_corrupt_and_atomic_replace_failure_preserve_last_valid_file(self) -> None:
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(DesktopSettingsError, "JSON 已损坏"):
            self.store.load()
        self.path.unlink()
        defaults = self.store.load()
        self.store.save(defaults)
        old_bytes = self.path.read_bytes()
        with patch("myagent.gui_models.os.replace", side_effect=OSError("locked")):
            with self.assertRaisesRegex(DesktopSettingsError, "无法保存桌面设置"):
                self.store.save(defaults)
        self.assertEqual(self.path.read_bytes(), old_bytes)


class ModelStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config" / "models.json"
        self.now = datetime(2026, 8, 6, 2, 3, 4, tzinfo=timezone.utc)
        self.store = ModelStore(self.path, clock=lambda: self.now)

    def test_missing_is_empty_and_data_home_precedence_stays_outside_repo(self) -> None:
        self.assertEqual(self.store.load(), [])
        with patch.dict(os.environ, {
            "MYAGENT_HOME": str(Path(self.temp.name) / "new"),
            "MYAGENT_GUI_HOME": str(Path(self.temp.name) / "gui"),
            "APPDATA": str(Path(self.temp.name) / "appdata"),
        }, clear=True):
            self.assertEqual(
                default_model_store_path(),
                (Path(self.temp.name) / "new" / "models.json").resolve(),
            )
        with patch.dict(os.environ, {
            "MYAGENT_GUI_HOME": str(Path(self.temp.name) / "gui"),
            "APPDATA": str(Path(self.temp.name) / "appdata"),
        }, clear=True):
            self.assertEqual(
                default_model_store_path(),
                (Path(self.temp.name) / "gui" / "models.json").resolve(),
            )
        with patch.dict(os.environ, {
            "APPDATA": str(Path(self.temp.name) / "appdata"),
        }, clear=True):
            self.assertEqual(
                default_model_store_path(),
                (Path(self.temp.name) / "appdata" / "MyAgent" / "models.json").resolve(),
            )

    def test_add_reload_created_at_delete_and_safe_repr(self) -> None:
        record = self.store.add(
            "gpt-test",
            "sk-super-secret",
            "https://gateway.example/v1",
        )
        self.assertEqual(record.created_at, "2026-08-06T02:03:04Z")
        reloaded = ModelStore(self.path).load()
        self.assertEqual(reloaded, [record])
        self.assertNotIn("sk-super-secret", repr(record))
        self.assertNotIn("sk-super-secret", repr(reloaded))
        self.assertEqual(record.display, ("gpt-test", record.created_at))
        self.assertEqual(record.base_url, "https://gateway.example/v1")
        self.assertTrue(self.store.delete("gpt-test"))
        self.assertFalse(self.store.delete("gpt-test"))
        self.assertEqual(self.store.load(), [])

    def test_duplicate_and_invalid_values_have_stable_errors_without_key(self) -> None:
        self.store.add("same", "sk-secret")
        with self.assertRaisesRegex(ModelValidationError, "模型名称已存在") as duplicate:
            self.store.add("same", "another-secret")
        self.assertNotIn("another-secret", str(duplicate.exception))
        invalid = (
            ("", "key", "模型名不能为空"),
            ("x" * (MAX_MODEL_NAME_LENGTH + 1), "key", "模型名不能超过"),
            ("ok", "", "API key 不能为空"),
            ("ok", "a b", "API key 不能包含空白"),
            ("ok", "x" * (MAX_API_KEY_LENGTH + 1), "API key 不能超过"),
        )
        for name, key, message in invalid:
            with self.subTest(message=message), self.assertRaisesRegex(
                ModelValidationError,
                message,
            ):
                self.store.add(name, key)

        invalid_urls = (
            ("ftp://example.test/v1", "完整的 HTTP"),
            ("example.test/v1", "完整的 HTTP"),
            ("https://user:secret@example.test/v1", "用户名或密码"),
            ("https://example.test/v1?token=secret", "查询参数或片段"),
            ("https://example.test/v1#anchor", "查询参数或片段"),
            ("https://example.test/" + "x" * MAX_BASE_URL_LENGTH, "不能超过"),
        )
        for base_url, message in invalid_urls:
            with self.subTest(base_url=base_url), self.assertRaisesRegex(
                ModelValidationError,
                message,
            ):
                self.store.add("valid", "key", base_url)

    def test_legacy_record_without_base_url_loads_as_default_endpoint(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps(
                {
                    "models": [
                        {
                            "name": "legacy",
                            "api_key": "legacy-secret",
                            "created_at": "2026-08-06T02:03:04Z",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        record = self.store.load()[0]
        self.assertIsNone(record.base_url)

    def test_atomic_replace_failure_preserves_old_file(self) -> None:
        self.store.add("old", "old-secret")
        old_bytes = self.path.read_bytes()
        with patch("myagent.gui_models.os.replace", side_effect=OSError("locked")):
            with self.assertRaisesRegex(ModelStoreError, "无法保存模型配置"):
                self.store.add("new", "new-secret")
        self.assertEqual(self.path.read_bytes(), old_bytes)
        self.assertEqual([record.name for record in self.store.load()], ["old"])
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_corrupt_json_and_invalid_schema_are_explicit(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ModelStoreError, "JSON 已损坏"):
            self.store.load()
        self.path.write_text(json.dumps({"models": [{"name": "x"}]}), encoding="utf-8")
        with self.assertRaisesRegex(ModelStoreError, "第 1 项"):
            self.store.load()


if __name__ == "__main__":
    unittest.main()
