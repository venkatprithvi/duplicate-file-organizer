from __future__ import annotations

from pathlib import Path

# Known OS-generated metadata/cache files. These are ignored by default, but
# every ignored file is recorded in the scan audit/report.
KNOWN_METADATA = {
    ".ds_store": "macOS Finder metadata",
    ".appledouble": "macOS AppleDouble metadata",
    ".lsoverride": "macOS Launch Services metadata",
    "thumbs.db": "Windows thumbnail cache",
    "ehthumbs.db": "Windows thumbnail cache",
    "desktop.ini": "Windows folder metadata",
    "iconcache.db": "Windows icon cache",
    ".duplicate_file_organizer.sqlite": "Duplicate File Organizer internal database",
    ".duplicate_file_organizer.sqlite-wal": "Duplicate File Organizer SQLite write-ahead log",
    ".duplicate_file_organizer.sqlite-shm": "Duplicate File Organizer SQLite shared-memory file",
}

def metadata_reason(path: Path) -> str | None:
    name = path.name.casefold()
    if name.startswith("._"):
        return "macOS AppleDouble metadata"
    return KNOWN_METADATA.get(name)
