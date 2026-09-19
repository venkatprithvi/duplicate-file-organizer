from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Classification = Literal["UNIQUE", "ORIGINAL", "DUPLICATE"]

@dataclass(slots=True)
class IgnoredFile:
    source_path: Path
    relative_path: str
    filename: str
    size_bytes: int
    modified_ns: int
    reason: str

@dataclass(slots=True)
class FileRecord:
    file_id: int
    source_path: Path
    relative_path: str
    original_filename: str
    extension: str
    size_bytes: int
    modified_ns: int
    partial_hash: str | None = None
    sha256: str | None = None
    classification: Classification | None = None
    group_id: int | None = None
    sequence: int | None = None
    metadata_reason: str | None = None
    planned_relative_destination: str | None = None
    prevalidated_hash: str | None = None
    destination_hash: str | None = None
    processing_status: str = "PENDING"
    error_message: str | None = None
    copy_started_at: str | None = None
    copy_completed_at: str | None = None
    copy_duration_seconds: float | None = None
    creation_year: str | None = None
    creation_location: str | None = None
    location_source: str | None = None
    creation_date_source: str | None = None
    creation_date_confidence: str | None = None

    @property
    def processing_eligible(self) -> bool:
        return True
