from __future__ import annotations
import os
import sys
from pathlib import Path

APP_NAME = "DuplicateFileOrganizer"

def app_data_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    result = base / APP_NAME
    result.mkdir(parents=True, exist_ok=True)
    return result

def database_path() -> Path:
    return app_data_dir() / "duplicate_file_organizer_v0_9_ignored_metadata.sqlite"
