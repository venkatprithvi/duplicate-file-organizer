from __future__ import annotations
from collections import defaultdict
from pathlib import Path
from .exclusions import metadata_reason
from .hashing import partial_sha256_file, sha256_file
from .metadata import extract_creation_metadata
from .models import FileRecord, IgnoredFile

class ScanCancelled(Exception): pass

class Scanner:
    def __init__(self, root: Path, *, follow_symlinks=False, include_hidden=True, ignore_system_metadata=False):
        self.root=Path(root).resolve(); self.follow_symlinks=follow_symlinks; self.include_hidden=include_hidden; self.ignore_system_metadata=ignore_system_metadata
        self.errors=[]; self.ignored_files=[]
    def _check_cancel(self,cancelled):
        if cancelled and cancelled(): raise ScanCancelled()
    def discover(self, progress=None, cancelled=None, logger=None):
        records=[]; next_id=1
        def walk(directory):
            nonlocal next_id
            self._check_cancel(cancelled)
            try: entries=sorted(directory.iterdir(),key=lambda p:(p.name.casefold(),p.name))
            except OSError as exc: self.errors.append((str(directory),str(exc))); return
            for entry in entries:
                self._check_cancel(cancelled)
                if not self.include_hidden and entry.name.startswith('.'): continue
                try:
                    if entry.is_symlink() and not self.follow_symlinks: continue
                    if entry.is_dir(): walk(entry); continue
                    if not entry.is_file(): continue
                    st=entry.stat(); reason=metadata_reason(entry)
                    if self.ignore_system_metadata and reason:
                        self.ignored_files.append(IgnoredFile(entry,entry.relative_to(self.root).as_posix(),entry.name,st.st_size,st.st_mtime_ns,reason)); continue
                    year,location,loc_source,date_source,date_confidence=extract_creation_metadata(entry)
                    r=FileRecord(next_id,entry,entry.relative_to(self.root).as_posix(),entry.name,entry.suffix.lower(),st.st_size,st.st_mtime_ns,creation_year=year,creation_location=location,location_source=loc_source,creation_date_source=date_source,creation_date_confidence=date_confidence)
                    records.append(r); next_id+=1
                    if progress: progress(len(records),0,str(entry))
                except OSError as exc: self.errors.append((str(entry),str(exc)))
        walk(self.root); return records
    def analyze(self,records,progress=None,cancelled=None,logger=None):
        by_size=defaultdict(list)
        for r in records: by_size[r.size_bytes].append(r)
        groups=[]
        for same in by_size.values():
            self._check_cancel(cancelled)
            if len(same)<2: continue
            by_partial=defaultdict(list)
            for r in same:
                self._check_cancel(cancelled)
                try:
                    r.partial_hash=partial_sha256_file(r.source_path); by_partial[r.partial_hash].append(r)
                    if progress: progress(0,0,f"Partial hash: {r.source_path}")
                except OSError as exc: self.errors.append((str(r.source_path),str(exc)))
            for candidates in by_partial.values():
                if len(candidates)<2: continue
                by_full=defaultdict(list)
                for r in candidates:
                    self._check_cancel(cancelled)
                    try:
                        r.sha256=sha256_file(r.source_path); by_full[r.sha256].append(r)
                        if progress: progress(0,0,f"SHA-256: {r.source_path}")
                    except OSError as exc: self.errors.append((str(r.source_path),str(exc)))
                groups.extend(g for g in by_full.values() if len(g)>=2)
        groups.sort(key=lambda g:min(str(r.source_path).casefold() for r in g))
        for gid,g in enumerate(groups,1):
            # Representative rule: oldest filesystem modification time, then deterministic path.
            g.sort(key=lambda r:(r.modified_ns,str(r.source_path).casefold(),r.relative_path))
            for seq,r in enumerate(g,1): r.group_id=gid; r.sequence=seq; r.classification='ORIGINAL' if seq==1 else 'DUPLICATE'
        duplicate_ids={r.file_id for g in groups for r in g}
        for r in records:
            if r.file_id not in duplicate_ids: r.classification='UNIQUE'
        return groups
