# Duplicate File Organizer — Phase 1.0.1

Cross-platform Python desktop application for **safe duplicate detection and copy-only organization** on Windows and macOS.

## What this version fixes

Phase 1.0.1 is a stability correction for the crash observed during:

**Review & Process → choose destination → Year → Location → Confirm and Start**

The GUI execution model has been changed from a `QObject` worker moved into a `QThread` to dedicated `QThread` subclasses. This avoids releasing a worker QObject while its thread is stopping, a lifecycle pattern that can cause native Qt/Python crashes on macOS.

The package also fixes two processing correctness issues found during the code review:

- destination count/size reconciliation is always against the **complete expected source population**; a failed copy cannot be hidden by reducing the expected count;
- destination duplicate-deletion reports capture file metadata before deletion, so reports do not try to stat files after they have been removed.

## Safety contract

The application:

- recursively scans regular files;
- includes hidden user files;
- **processes all regular files by default**, including `.DS_Store`;
- provides an optional checkbox to ignore recognized OS-generated metadata files;
- does not move, rename, delete, or modify source files during the copy workflow;
- detects exact duplicates using size → partial SHA-256 → full SHA-256;
- assigns deterministic ORIGINAL and DUPLICATE sequence values;
- copies every discovered processing file;
- validates source count, total size, destination mappings, readability and source stability before copying when pre-validation is enabled;
- copies through a same-directory temporary file and atomically replaces the planned destination;
- performs mandatory lightweight destination count/size reconciliation after copying;
- optionally performs full post-copy validation and SHA-256 verification;
- writes CSV and JSON audit reports;
- stores recovery state in SQLite outside the source folder;
- supports interrupted/failed-job resume;
- keeps destination-only duplicate deletion as a separate workflow.

## Destination organization

### Flat

```text
DuplicateReview_YYYYMMDD_HHMMSS/
├── G000001_01_ORIGINAL_report.pdf
├── G000001_02_DUPLICATE_report.pdf
├── U000003_UNIQUE_photo.jpg
└── reports/
```

### Year → Location

```text
DuplicateReview_YYYYMMDD_HHMMSS/
├── 2024/
│   └── GPS_17.70000_83.30000/
│       └── U000003_UNIQUE_photo.jpg
├── Unknown Year/
│   └── Unknown Location/
│       └── U000004_UNIQUE_document.pdf
└── reports/
```

For images, creation year prefers EXIF capture time. Location uses EXIF GPS coordinates. No online reverse geocoding is performed. For files without usable image EXIF, the year falls back to filesystem creation time, or modification time where creation time is unavailable.

## Destination-only duplicate deletion

The **Delete Duplicates from Destination** workflow:

1. analyzes only the selected destination;
2. identifies exact duplicates using size + SHA-256;
3. shows a review dialog;
4. requires explicit confirmation;
5. re-checks the candidate hash immediately before deletion;
6. deletes only files inside the selected destination;
7. never deletes from the source folder;
8. writes `delete_review.csv`, `delete_status.csv`, and `delete_summary.csv`.

## Database location

The SQLite database is outside the source folder:

- macOS: `~/Library/Application Support/DuplicateFileOrganizer/`
- Windows: `%LOCALAPPDATA%\\DuplicateFileOrganizer\\`

## Install and run

Use your project-specific Python 3.12 virtual environment; the application does not require changing your system/default Python.

```bash
source .venv/bin/activate
python --version
python -m pip install -e ".[dev]"
python -m pytest -q
python -m duplicate_file_organizer.main
```

On Windows, activate the project's Windows virtual environment and run the equivalent commands.

## Packaging

PyInstaller must be run on the target operating system. Build the macOS application on macOS and the Windows executable on Windows.

```text
macOS:   ./build_mac.sh
Windows: build_windows.bat
```

## Verification

The Phase 1.0.1 package was checked with the project's automated core suite, Python bytecode compilation, and a direct end-to-end Year → Location copy/reconciliation/SHA-256 test.

The supplied execution environment cannot launch the macOS Qt GUI itself, so the actual macOS GUI crash fix must still be exercised on the Mac. The new architecture is specifically designed to remove the worker-lifecycle race involved in the previous GUI implementation.


## Phase 1.1 — Metadata-aware Year → Location organization

The stable Flat copy and destination-only duplicate deletion workflows are preserved. Year → Location adds a metadata resolver with this priority:

1. Image EXIF original/digitized/metadata date; EXIF GPS remains local.
2. Office Open XML `docProps/core.xml` created date.
3. PDF CreationDate/XMP creation date.
4. MP4/MOV/3GP QuickTime `mvhd` creation time.
5. Matroska/WebM DateUTC when safely parseable.
6. Filesystem birth/creation time as a LOW-confidence fallback.
7. Filesystem modified time as the final LOW-confidence fallback.
8. `Unknown Year` if no usable date exists.

Every record stores the date source and confidence. Metadata errors never stop copying. A single copy error is recorded and processing continues with the next file. No online reverse geocoding is performed.

Destination example:

```text
Destination/
  2019/
    GPS_17.68680_83.21850/
      G000001_01_ORIGINAL_photo.jpg
  2020/
    Unknown Location/
      U000002_UNIQUE_document.pdf
  Unknown Year/
    Unknown Location/
      U000003_UNIQUE_unknown.bin
```
