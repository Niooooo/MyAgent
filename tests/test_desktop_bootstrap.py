from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = ROOT / "scripts" / "bootstrap_desktop.py"


def load_bootstrap():
    spec = importlib.util.spec_from_file_location("desktop_bootstrap", BOOTSTRAP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("desktop bootstrap module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DesktopBootstrapTests(unittest.TestCase):
    def test_runtime_home_uses_explicit_override_then_local_appdata(self) -> None:
        bootstrap = load_bootstrap()
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            with patch.dict(os.environ, {
                "MYAGENT_RUNTIME_HOME": str(base / "override"),
                "LOCALAPPDATA": str(base / "local"),
            }, clear=True):
                self.assertEqual(bootstrap.runtime_home(), (base / "override").resolve())
            with patch.dict(os.environ, {"LOCALAPPDATA": str(base / "local")}, clear=True):
                self.assertEqual(
                    bootstrap.runtime_home(),
                    (base / "local" / "MyAgent" / "runtime").resolve(),
                )

    def test_dependency_fingerprint_changes_with_project_or_pyproject(self) -> None:
        bootstrap = load_bootstrap()
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "first"
            second = Path(temp) / "second"
            first.mkdir()
            second.mkdir()
            (first / "pyproject.toml").write_text("dependencies = []\n", encoding="utf-8")
            (second / "pyproject.toml").write_text("dependencies = []\n", encoding="utf-8")
            first_hash = bootstrap.dependency_fingerprint(first)
            self.assertNotEqual(first_hash, bootstrap.dependency_fingerprint(second))
            (first / "pyproject.toml").write_text(
                'dependencies = ["croniter"]\n', encoding="utf-8"
            )
            self.assertNotEqual(first_hash, bootstrap.dependency_fingerprint(first))


if __name__ == "__main__":
    unittest.main()
