"""Prepare a stable, update-safe Python runtime for the desktop launcher."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path


BOOTSTRAP_VERSION = "1"


def runtime_home() -> Path:
    override = os.getenv("MYAGENT_RUNTIME_HOME")
    if override:
        return Path(override).expanduser().resolve()
    local_appdata = os.getenv("LOCALAPPDATA")
    if local_appdata:
        return (Path(local_appdata).expanduser().resolve() / "MyAgent" / "runtime").resolve()
    return (Path.home() / "AppData" / "Local" / "MyAgent" / "runtime").resolve()


def runtime_python(home: Path | None = None) -> Path:
    selected_home = home or runtime_home()
    if os.name == "nt":
        return selected_home / "venv" / "Scripts" / "python.exe"
    return selected_home / "venv" / "bin" / "python"


def dependency_fingerprint(project_root: Path) -> str:
    root = project_root.expanduser().resolve()
    digest = hashlib.sha256()
    digest.update(BOOTSTRAP_VERSION.encode("ascii"))
    digest.update(os.path.normcase(str(root)).encode("utf-8"))
    digest.update((root / "pyproject.toml").read_bytes())
    return digest.hexdigest()


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        stdout=sys.stderr,
        stderr=sys.stderr,
    )


def _healthy(interpreter: Path, project_root: Path) -> bool:
    if not interpreter.is_file():
        return False
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(project_root / "src"), env.get("PYTHONPATH", "")) if value
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [str(interpreter), "-X", "utf8", "-m", "myagent.desktop_sidecar", "--check"],
        cwd=project_root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _write_fingerprint(path: Path, value: str) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".dependencies.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_runtime(project_root: Path) -> Path:
    project = project_root.expanduser().resolve()
    home = runtime_home()
    home.mkdir(parents=True, exist_ok=True)
    interpreter = runtime_python(home)
    fingerprint_path = home / "dependencies.sha256"
    expected = dependency_fingerprint(project)
    current = ""
    try:
        current = fingerprint_path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        pass
    except OSError:
        current = ""

    if not interpreter.is_file():
        _run([sys.executable, "-m", "venv", str(home / "venv")], cwd=project)

    if current != expected or not _healthy(interpreter, project):
        _run(
            [
                str(interpreter),
                "-X",
                "utf8",
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--upgrade",
                "--editable",
                str(project),
            ],
            cwd=project,
        )
        if not _healthy(interpreter, project):
            raise RuntimeError("desktop Python runtime failed its import check after installation")
        _write_fingerprint(fingerprint_path, expected)
    return interpreter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare the MyAgent desktop Python runtime")
    parser.add_argument("--project-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        interpreter = ensure_runtime(args.project_root)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[MyAgent] Python runtime preparation failed: {exc}", file=sys.stderr)
        return 1
    print(interpreter)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
