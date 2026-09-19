from __future__ import annotations
import re, unicodedata
from pathlib import PurePath
from .models import FileRecord

WINDOWS_RESERVED={"CON","PRN","AUX","NUL",*(f"COM{i}" for i in range(1,10)),*(f"LPT{i}" for i in range(1,10))}
INVALID=re.compile(r'[<>:"/\\|?*\x00-\x1f]')

def sanitize_component(value: str, max_length: int = 100) -> str:
    value=INVALID.sub("_", value); value=re.sub(r"\s+", " ", value).strip().rstrip(". ") or "Unknown"
    if value.upper() in WINDOWS_RESERVED: value=f"_{value}_"
    return value[:max_length].rstrip(". ") or "Unknown"

def sanitize_filename(filename: str, max_length: int = 180) -> str:
    path=PurePath(filename); stem,suffix=path.stem,path.suffix
    stem=INVALID.sub("_",stem); stem=re.sub(r"\s+"," ",stem).strip().rstrip(". ") or "unnamed"
    if stem.upper() in WINDOWS_RESERVED: stem=f"_{stem}_"
    stem=stem[:max(1,max_length-len(suffix))].rstrip(". ") or "unnamed"
    return f"{stem}{suffix}"

def collision_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()

def destination_relative_path(record: FileRecord, organization: str = "FLAT") -> str:
    original=sanitize_filename(record.original_filename)
    if record.classification in {"ORIGINAL","DUPLICATE"}:
        prefix=f"G{record.group_id:06d}_{record.sequence:02d}_{record.classification}_"
    else:
        prefix=f"U{record.file_id:06d}_UNIQUE_"
    filename=prefix+original
    if organization == "YEAR_LOCATION":
        year=sanitize_component(record.creation_year or "Unknown Year")
        loc=sanitize_component(record.creation_location or "Unknown Location")
        return f"{year}/{loc}/{filename}"
    return filename

def apply_destination_names(records: list[FileRecord], organization: str = "FLAT") -> list[FileRecord]:
    used=set()
    for record in sorted(records,key=lambda x:x.file_id):
        candidate=destination_relative_path(record,organization)
        if collision_key(candidate) in used:
            p=PurePath(candidate); n=2
            while collision_key(candidate) in used:
                candidate=str(p.parent / f"{p.stem}_{n}{p.suffix}"); n+=1
        record.planned_relative_destination=candidate; used.add(collision_key(candidate))
    return records
