# Phase 0.9 Final — engineering review (flat destination layout)

## Defect fixed from the reported screenshot

The screenshot showed:

- source count = expected count;
- total size = expected size;
- missing files = none;
- unexpected files = none;
- size mismatches = none;
- `extension_ok = False`.

The cause was the `.DS_Store` filename. The source file is a dot-file with no conventional extension, but after prefixing it becomes a filename such as:

```text
G000001_01_ORIGINAL_.DS_Store
```

A raw `Path.suffix()` call sees `.DS_Store` as the suffix of the generated destination filename. The final implementation no longer uses that incorrect inference. It validates the planned destination filename and treats the source `.DS_Store` as a no-extension file.

## Additional defects found during code review

The final review also corrected several recovery/database issues that were not visible in the screenshot:

1. **SQLite file ID collision across scans** — file IDs were local to a scan while the old schema made them globally primary-keyed. The final schema uses `(scan_id, file_id)` as the file identity.
2. **Resume job ID bug** — the previous GUI passed the complete resume-job dictionary where an integer job ID was required. The final code uses `resume_job["job_id"]`.
3. **Resume disk-space calculation** — resume now calculates only bytes still requiring a copy after source validation and hash capture.
4. **Partial destination files** — copies are made to same-directory temporary files and atomically replaced into the final path.
5. **Corrupt resume targets** — an existing target with the correct planned path can be safely replaced by the newly verified copy.
6. **Old SQLite schema reuse** — the final build uses a versioned application database filename.
7. **Unicode/case destination collisions** — destination collision keys use NFC normalization plus casefolding.
8. **Console entry point** — `main.py` now provides the `run()` function declared by `pyproject.toml`.

## Source safety

The processing path is explicitly copy-only. The implementation does not call `shutil.move`, `unlink` or `rmtree` against the source. Python's `shutil.copy2()` is used for the data copy and metadata preservation attempt; Python documents that `copy2()` cannot preserve every platform-specific metadata item, so the application treats SHA-256 of file content as the final integrity check.

## Recovery model

```text
PENDING
  → COPYING
  → COPIED
  → VERIFIED
  → COMPLETED

Failure/cancellation
  → INTERRUPTED / FAILED
  → Resume Last Job
```

A resume never assumes an existing destination file is correct solely from size. It checks its SHA-256 against the prevalidated source hash before skipping the copy.


## Default OS metadata exclusion

Phase 0.9.3 now distinguishes **hidden files** from **OS-generated system/metadata files**. The application still scans hidden user files, but ignores a curated list of known OS-generated files by default, including macOS `.DS_Store` and Windows `Thumbs.db`/`desktop.ini`.

Ignored files are never modified, moved, or deleted. Every ignored item is stored in the SQLite scan audit and included in the CSV/JSON report. The GUI provides **View Ignored System Files** after a scan.

This prevents Finder/Windows metadata from polluting duplicate groups and count/extension validation while preserving transparency.


## Flat destination layout

Processed data files are written directly under the generated `DuplicateReview_YYYYMMDD_HHMMSS` process folder. Duplicate group, sequence, classification, unique ID and original filename details are encoded in the filename. No `G000001/` or `UNIQUE/` data subfolders are created. The `reports/` folder is the only subfolder and contains audit reports.

Examples:

```text
DuplicateReview_20260917_185500/
  G000001_01_ORIGINAL_report.pdf
  G000001_02_DUPLICATE_report-copy.pdf
  U000003_UNIQUE_notes.txt
  reports/
    process_report.csv
    process_manifest.json
```


### Application database exclusion

The legacy source-folder file `.duplicate_file_organizer.sqlite` is the application's internal SQLite database from older builds. It is not user data and must not be copied into a processing destination. The scanner ignores this exact filename and its SQLite `-wal` and `-shm` sidecars by default and records them in the ignored-file audit. If the system-metadata filter is explicitly disabled, these files are treated like ordinary files.
