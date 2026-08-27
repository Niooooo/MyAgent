"""Stable user-data paths shared by CLI and desktop persistence."""

from __future__ import annotations

import os
from pathlib import Path


def default_data_home() -> Path:
    """Return one repository-external root for every durable user setting."""
    override = os.getenv("MYAGENT_HOME") or os.getenv("MYAGENT_GUI_HOME")
    if override:
        return Path(override).expanduser().resolve()
    appdata = os.getenv("APPDATA")
    if appdata:
        return (Path(appdata).expanduser().resolve() / "MyAgent").resolve()
    return (Path.home() / "AppData" / "Roaming" / "MyAgent").resolve()
