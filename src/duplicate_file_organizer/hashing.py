from __future__ import annotations
import hashlib
from pathlib import Path

CHUNK_SIZE = 1024 * 1024
PARTIAL_BYTES = 64 * 1024

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()

def partial_sha256_file(path: Path, sample_bytes: int = PARTIAL_BYTES) -> str:
    size = path.stat().st_size
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        if size <= sample_bytes * 2:
            digest.update(handle.read())
        else:
            digest.update(handle.read(sample_bytes))
            handle.seek(-sample_bytes, 2)
            digest.update(handle.read(sample_bytes))
    return digest.hexdigest()
