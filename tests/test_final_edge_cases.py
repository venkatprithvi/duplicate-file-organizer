from __future__ import annotations

import os
from pathlib import Path

import pytest

from duplicate_file_organizer.database import Database
from duplicate_file_organizer.hashing import sha256_file
from duplicate_file_organizer.models import FileRecord, IgnoredFile
from duplicate_file_organizer.naming import apply_destination_names, sanitize_filename
from duplicate_file_organizer.processor import (
    ValidationError,
    copy_one,
    extension_counts,
    make_process_folder,
    post_validate,
    pre_validate,
    resume_required_bytes,
    verify_hashes,
)
from duplicate_file_organizer.scanner import Scanner


def scan_and_name(root: Path):
    scanner = Scanner(root)
    records = scanner.discover()
    groups = scanner.analyze(records)
    apply_destination_names(records)
    return scanner, records, groups


def test_dotfile_extension_and_post_validation(tmp_path):
    src = tmp_path / "src"
    parent = tmp_path / "out"
    src.mkdir(); parent.mkdir()
    (src / ".gitignore").write_text("*.tmp", encoding="utf-8")
    (src / "a.txt").write_text("hello", encoding="utf-8")

    scanner, records, _ = scan_and_name(src)
    assert scanner.errors == []
    assert extension_counts(records)["<NO_EXTENSION>"] == 1

    pre = pre_validate(src, records, parent)
    dest = make_process_folder(parent)
    db = Database(tmp_path / "db.sqlite")
    job = db.create_job(1, src, dest, records) if False else None
    # Build destination directly for post-validation coverage.
    for r in records:
        target = dest / r.planned_relative_destination
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(r.source_path.read_bytes())
    post = post_validate(records, dest)
    assert post["status"] == "PASS"
    assert post["extension_ok"] is True
    db.close()


def test_processed_data_files_are_flat_in_destination(tmp_path):
    src = tmp_path / "src"; parent = tmp_path / "out"
    src.mkdir(); parent.mkdir()
    (src / "original.pdf").write_bytes(b"same")
    (src / "duplicate.pdf").write_bytes(b"same")
    (src / "unique.txt").write_bytes(b"unique")
    scanner, records, groups = scan_and_name(src)
    assert len(groups) == 1
    pre_validate(src, records, parent)
    dest = make_process_folder(parent)
    db = Database(tmp_path / "db.sqlite")
    scan_id = db.create_scan(src); db.save_scan(scan_id, records, groups, 0)
    job = db.create_job(scan_id, src, dest, records)
    for r in records:
        copy_one(r, dest, db, job, lambda: False)
    data_files = [p for p in dest.iterdir() if p.is_file()]
    assert len(data_files) == len(records)
    assert all(p.parent == dest for p in data_files)
    assert all("/" not in r.planned_relative_destination and "\\" not in r.planned_relative_destination for r in records)
    assert post_validate(records, dest)["status"] == "PASS"
    db.close()


def test_post_validation_rejects_unexpected_file(tmp_path):
    src = tmp_path / "src"; parent = tmp_path / "out"
    src.mkdir(); parent.mkdir(); (src / "a.txt").write_text("abc")
    _, records, _ = scan_and_name(src)
    pre_validate(src, records, parent)
    dest = make_process_folder(parent)
    target = dest / records[0].planned_relative_destination
    target.write_text("abc")
    (dest / "unexpected.bin").write_bytes(b"x")
    result = post_validate(records, dest)
    assert result["status"] == "FAIL"
    assert "unexpected.bin" in result["unexpected"]


def test_post_validation_ignores_reports_directory(tmp_path):
    src = tmp_path / "src"; parent = tmp_path / "out"
    src.mkdir(); parent.mkdir(); (src / "a.txt").write_text("abc")
    _, records, _ = scan_and_name(src)
    pre_validate(src, records, parent)
    dest = make_process_folder(parent)
    target = dest / records[0].planned_relative_destination
    target.write_text("abc")
    (dest / "reports").mkdir()
    (dest / "reports" / "process_report.csv").write_text("report")
    assert post_validate(records, dest)["status"] == "PASS"


def test_zero_byte_file_hash_and_copy(tmp_path):
    src = tmp_path / "src"; parent = tmp_path / "out"
    src.mkdir(); parent.mkdir(); (src / "empty.bin").touch()
    _, records, _ = scan_and_name(src)
    pre_validate(src, records, parent)
    dest = make_process_folder(parent)
    db = Database(tmp_path / "db.sqlite")
    scan_id = db.create_scan(src); db.save_scan(scan_id, records, [], 0)
    job = db.create_job(scan_id, src, dest, records)
    copy_one(records[0], dest, db, job, lambda: False)
    assert (dest / records[0].planned_relative_destination).stat().st_size == 0
    assert sha256_file(dest / records[0].planned_relative_destination) == sha256_file(src / "empty.bin")
    db.close()


def test_duplicate_detection_three_files_and_unique(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"same")
    (tmp_path / "b.txt").write_bytes(b"same")
    (tmp_path / "c.txt").write_bytes(b"same")
    (tmp_path / "unique.txt").write_bytes(b"different")
    scanner, records, groups = scan_and_name(tmp_path)
    assert not scanner.errors
    assert len(groups) == 1
    assert sorted(r.classification for r in records).count("ORIGINAL") == 1
    assert sorted(r.classification for r in records).count("DUPLICATE") == 2
    assert sorted(r.classification for r in records).count("UNIQUE") == 1
    assert len({r.planned_relative_destination.casefold() for r in records}) == len(records)


def test_case_and_sanitization_collision_is_deterministic(tmp_path):
    records = []
    for i, name in enumerate(["a:b.txt", "a?b.txt", "CON.txt"], 1):
        p = tmp_path / name
        p.write_text(name)
        records.append(FileRecord(i, p, name, name, p.suffix, p.stat().st_size, p.stat().st_mtime_ns, classification="UNIQUE"))
    apply_destination_names(records)
    destinations = [r.planned_relative_destination for r in records]
    assert len({d.casefold() for d in destinations}) == 3
    assert sanitize_filename("CON.txt") == "_CON_.txt"
    assert sanitize_filename("a:b.txt") == sanitize_filename("a?b.txt")
    assert destinations[0] != destinations[1]


def test_reserved_and_long_filename_are_safe(tmp_path):
    long_name = "x" * 500 + ".txt"
    safe = sanitize_filename(long_name)
    assert len(safe) <= 180
    assert safe.endswith(".txt")
    assert sanitize_filename("NUL") == "_NUL_"
    assert sanitize_filename("LPT1.txt") == "_LPT1_.txt"


def test_source_change_is_rejected(tmp_path):
    src = tmp_path / "src"; parent = tmp_path / "out"
    src.mkdir(); parent.mkdir(); p = src / "a.txt"; p.write_text("one")
    _, records, _ = scan_and_name(src)
    p.write_text("two")
    with pytest.raises(ValidationError):
        pre_validate(src, records, parent)


def test_source_destination_overlap_both_directions(tmp_path):
    src = tmp_path / "src"; src.mkdir(); (src / "a.txt").write_text("a")
    _, records, _ = scan_and_name(src)
    with pytest.raises(ValidationError):
        pre_validate(src, records, src / "child")
    outside = tmp_path / "outside"; outside.mkdir()
    # Source is inside destination parent: also prohibited because processing
    # would recursively place output alongside input population.
    nested_source = outside / "nested"; nested_source.mkdir(); (nested_source / "a").write_text("a")
    _, rec2, _ = scan_and_name(nested_source)
    with pytest.raises(ValidationError):
        pre_validate(nested_source, rec2, outside)


def test_resume_required_bytes_counts_only_missing_or_bad_files(tmp_path):
    src = tmp_path / "src"; parent = tmp_path / "out"
    src.mkdir(); parent.mkdir()
    (src / "a.bin").write_bytes(b"aaaa")
    (src / "b.bin").write_bytes(b"bbbbbb")
    _, records, _ = scan_and_name(src)
    pre_validate(src, records, parent)
    dest = make_process_folder(parent)
    valid = dest / records[0].planned_relative_destination
    valid.write_bytes(b"aaaa")
    records[0].prevalidated_hash = sha256_file(src / "a.bin")
    bad = dest / records[1].planned_relative_destination
    bad.parent.mkdir(parents=True, exist_ok=True); bad.write_bytes(b"xx")
    assert resume_required_bytes(records, dest) == records[1].size_bytes


def test_database_keeps_file_ids_isolated_between_scans(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    src = tmp_path / "src"; src.mkdir()
    p = src / "a.txt"; p.write_text("a")

    scan1 = db.create_scan(src)
    r1 = FileRecord(1, p, "a.txt", "a.txt", ".txt", 1, p.stat().st_mtime_ns, classification="UNIQUE", planned_relative_destination="U000001_UNIQUE_a.txt")
    db.save_scan(scan1, [r1], [], 0)

    scan2 = db.create_scan(src)
    r2 = FileRecord(1, p, "a.txt", "a.txt", ".txt", 1, p.stat().st_mtime_ns, classification="UNIQUE", planned_relative_destination="U000001_UNIQUE_a.txt")
    db.save_scan(scan2, [r2], [], 0)

    count = db.connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert count == 2
    db.close()


def test_database_resume_returns_numeric_job_id(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    src = tmp_path / "src"; parent = tmp_path / "out"
    src.mkdir(); parent.mkdir(); p = src / "a.txt"; p.write_text("a")
    scan_id = db.create_scan(src)
    r = FileRecord(1, p, "a.txt", "a.txt", ".txt", 1, p.stat().st_mtime_ns, classification="UNIQUE", planned_relative_destination="U000001_UNIQUE_a.txt")
    db.save_scan(scan_id, [r], [], 0)
    dest = make_process_folder(parent)
    job = db.create_job(scan_id, src, dest, [r])
    db.set_job(job, "PROCESSING")
    resumed = db.resume_job()
    assert resumed is not None
    assert resumed["job_id"] == job
    assert isinstance(resumed["job_id"], int)
    db.close()


def test_symlink_is_not_treated_as_regular_file(tmp_path):
    real = tmp_path / "real.txt"; real.write_text("x")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable on this platform")
    scanner = Scanner(tmp_path, follow_symlinks=False)
    records = scanner.discover()
    assert [r.original_filename for r in records] == ["real.txt"]


def test_unicode_filename_and_nested_directory(tmp_path):
    nested = tmp_path / "資料" / "résumé"
    nested.mkdir(parents=True)
    p = nested / "данные.txt"
    p.write_text("unicode")
    scanner, records, _ = scan_and_name(tmp_path)
    assert len(records) == 1
    assert records[0].relative_path == "資料/résumé/данные.txt"
    assert records[0].planned_relative_destination


def test_end_to_end_copy_all_files_and_sha256(tmp_path):
    src = tmp_path / "source"
    parent = tmp_path / "destination_parent"
    src.mkdir(); parent.mkdir()
    (src / ".gitignore").write_text("*.tmp\n")
    (src / "empty").touch()
    (src / "same1.bin").write_bytes(b"duplicate-content" * 100)
    (src / "same2.bin").write_bytes(b"duplicate-content" * 100)
    nested = src / "nested"; nested.mkdir()
    (nested / "unicode-資料.txt").write_text("hello", encoding="utf-8")
    before = {p.relative_to(src).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns, sha256_file(p)) for p in src.rglob("*") if p.is_file()}

    scanner, records, groups = scan_and_name(src)
    assert len(records) == len(before)
    assert not scanner.ignored_files
    pre = pre_validate(src, records, parent)
    assert pre["status"] == "PASS"

    dest = make_process_folder(parent)
    db = Database(tmp_path / "app.sqlite")
    scan_id = db.create_scan(src)
    db.save_scan(scan_id, records, groups, 0)
    job = db.create_job(scan_id, src, dest, records)
    db.set_job(job, "PROCESSING")
    for r in records:
        copy_one(r, dest, db, job, lambda: False)
    post = post_validate(records, dest)
    assert post["status"] == "PASS"
    verified, failures = verify_hashes(records, dest, db, job, lambda: False)
    assert verified == len(records)
    assert failures == []
    for r in records:
        target = dest / r.planned_relative_destination
        assert sha256_file(target) == before[r.relative_path][2]
    after = {p.relative_to(src).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns, sha256_file(p)) for p in src.rglob("*") if p.is_file()}
    assert after == before
    db.close()


def test_copy_replaces_corrupt_resume_target_atomically(tmp_path):
    src = tmp_path / "src"; parent = tmp_path / "out"
    src.mkdir(); parent.mkdir(); (src / "a.bin").write_bytes(b"correct-data")
    _, records, _ = scan_and_name(src)
    pre_validate(src, records, parent)
    dest = make_process_folder(parent)
    target = dest / records[0].planned_relative_destination
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"corrupt")
    db = Database(tmp_path / "db.sqlite")
    scan_id = db.create_scan(src); db.save_scan(scan_id, records, [], 0)
    job = db.create_job(scan_id, src, dest, records); db.set_job(job, "PROCESSING")
    copy_one(records[0], dest, db, job, lambda: False)
    assert target.read_bytes() == b"correct-data"
    db.close()


def test_duplicate_group_original_choice_is_deterministic(tmp_path):
    a = tmp_path / "a.txt"; b = tmp_path / "b.txt"
    a.write_text("same"); b.write_text("same")
    # Force equal mtimes; path ordering becomes the deterministic tiebreaker.
    ns = 1_700_000_000_000_000_000
    os.utime(a, ns=(ns, ns)); os.utime(b, ns=(ns, ns))
    _, records, groups = scan_and_name(tmp_path)
    assert len(groups) == 1
    original = next(r for r in records if r.classification == "ORIGINAL")
    assert original.source_path.name == "a.txt"


from duplicate_file_organizer.exclusions import metadata_reason



def test_duplicate_file_organizer_internal_database_is_ignored_by_default(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    internal_db = src / ".duplicate_file_organizer.sqlite"
    internal_db.write_bytes(b"sqlite-internal")
    (src / ".duplicate_file_organizer.sqlite-wal").write_bytes(b"wal")
    (src / ".duplicate_file_organizer.sqlite-shm").write_bytes(b"shm")
    (src / "real.txt").write_text("real user data")

    scanner = Scanner(src, ignore_system_metadata=True)
    records = scanner.discover()

    assert [r.original_filename for r in records] == ["real.txt"]
    assert sorted(i.filename for i in scanner.ignored_files) == [
        ".duplicate_file_organizer.sqlite",
        ".duplicate_file_organizer.sqlite-shm",
        ".duplicate_file_organizer.sqlite-wal",
    ]
    assert all("Duplicate File Organizer" in i.reason for i in scanner.ignored_files)


def test_duplicate_file_organizer_internal_database_is_not_copied_to_flat_destination(tmp_path):
    src = tmp_path / "src"; parent = tmp_path / "out"
    src.mkdir(); parent.mkdir()
    (src / ".duplicate_file_organizer.sqlite").write_bytes(b"sqlite-internal")
    (src / ".duplicate_file_organizer.sqlite-wal").write_bytes(b"wal")
    (src / ".duplicate_file_organizer.sqlite-shm").write_bytes(b"shm")
    (src / "a.txt").write_text("user data")
    (src / "b.txt").write_text("same")
    (src / "c.txt").write_text("same")

    scanner = Scanner(src, ignore_system_metadata=True)
    records = scanner.discover()
    groups = scanner.analyze(records)
    apply_destination_names(records)
    pre = pre_validate(src, records, parent)
    assert pre["count"] == 3

    dest = make_process_folder(parent)
    db = Database(tmp_path / "test.sqlite")
    scan_id = db.create_scan(src)
    db.save_scan(scan_id, records, groups, len(scanner.errors), scanner.ignored_files)
    job = db.create_job(scan_id, src, dest, records)
    for record in records:
        copy_one(record, dest, db, job, lambda: False)

    data_files = [p.name for p in dest.iterdir() if p.is_file()]
    assert sorted(data_files) == sorted(r.planned_relative_destination for r in records)
    assert ".duplicate_file_organizer.sqlite" not in data_files
    assert ".duplicate_file_organizer.sqlite-wal" not in data_files
    assert ".duplicate_file_organizer.sqlite-shm" not in data_files
    assert post_validate(records, dest)["status"] == "PASS"
    db.close()


def test_internal_database_can_be_included_when_system_metadata_filter_is_disabled(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    (src / ".duplicate_file_organizer.sqlite").write_bytes(b"sqlite-internal")
    scanner = Scanner(src, ignore_system_metadata=False)
    records = scanner.discover()
    assert [r.original_filename for r in records] == [".duplicate_file_organizer.sqlite"]
    assert scanner.ignored_files == []


def test_os_metadata_files_are_ignored_and_audited_by_default(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    (src / ".DS_Store").write_bytes(b"finder")
    (src / "Thumbs.db").write_bytes(b"thumbs")
    (src / "desktop.ini").write_text("[.ShellClassInfo]")
    (src / ".gitignore").write_text("*.tmp")
    (src / "normal.txt").write_text("user file")
    scanner = Scanner(src, ignore_system_metadata=True)
    records = scanner.discover()
    assert sorted(r.original_filename for r in records) == [".gitignore", "normal.txt"]
    assert sorted(i.filename for i in scanner.ignored_files) == [".DS_Store", "Thumbs.db", "desktop.ini"]
    assert all(i.reason for i in scanner.ignored_files)

def test_system_metadata_exclusion_can_be_disabled(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    (src / ".DS_Store").write_bytes(b"finder")
    (src / "._important.txt").write_bytes(b"appledouble")
    scanner = Scanner(src, ignore_system_metadata=False)
    records = scanner.discover()
    assert len(records) == 2
    assert {r.original_filename for r in records} == {".DS_Store", "._important.txt"}
    assert scanner.ignored_files == []

def test_case_insensitive_known_metadata_names_are_ignored(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    (src / "THUMBS.DB").write_bytes(b"x")
    (src / "Desktop.Ini").write_bytes(b"x")
    scanner = Scanner(src, ignore_system_metadata=True)
    assert scanner.discover() == []
    assert len(scanner.ignored_files) == 2
    assert metadata_reason(src / "THUMBS.DB") == "Windows thumbnail cache"

def test_ignored_metadata_is_not_part_of_processing_population(tmp_path):
    src = tmp_path / "src"; parent = tmp_path / "out"
    src.mkdir(); parent.mkdir()
    (src / ".DS_Store").write_bytes(b"x" * 10)
    (src / "a.txt").write_bytes(b"abc")
    scanner = Scanner(src, ignore_system_metadata=True)
    records = scanner.discover(); scanner.analyze(records); apply_destination_names(records)
    assert len(records) == 1
    assert records[0].original_filename == "a.txt"
    pre = pre_validate(src, records, parent)
    assert pre["count"] == 1
    assert pre["total_size"] == 3


def test_appledouble_prefix_is_ignored_by_default(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    (src / "._document.pdf").write_bytes(b"resource-fork")
    (src / "document.pdf").write_bytes(b"real document")
    scanner = Scanner(src, ignore_system_metadata=True)
    records = scanner.discover()
    assert [r.original_filename for r in records] == ["document.pdf"]
    assert scanner.ignored_files[0].reason == "macOS AppleDouble metadata"


def test_ignored_files_are_persisted_in_scan_audit(tmp_path):
    src = tmp_path / "src"; src.mkdir()
    (src / ".DS_Store").write_bytes(b"finder")
    (src / "a.txt").write_text("abc")
    scanner = Scanner(src, ignore_system_metadata=True)
    records = scanner.discover(); groups = scanner.analyze(records); apply_destination_names(records)
    db = Database(tmp_path / "audit.sqlite")
    scan_id = db.create_scan(src)
    db.save_scan(scan_id, records, groups, len(scanner.errors), scanner.ignored_files)
    row = db.connection.execute("SELECT ignored_file_count,ignored_size_bytes FROM scan_sessions WHERE scan_id=?", (scan_id,)).fetchone()
    assert row == (1, 6)
    ignored = db.connection.execute("SELECT filename,reason FROM ignored_files WHERE scan_id=?", (scan_id,)).fetchall()
    assert ignored == [(".DS_Store", "macOS Finder metadata")]
    db.close()


def test_resume_job_restores_ignored_system_metadata_audit(tmp_path):
    src = tmp_path / "src"; parent = tmp_path / "out"
    src.mkdir(); parent.mkdir()
    ignored = IgnoredFile(src / ".DS_Store", ".DS_Store", ".DS_Store", 4, 123, "macOS Finder metadata")
    p = src / "a.txt"; p.write_text("abc")
    scanner = Scanner(src)
    records = scanner.discover(); groups = scanner.analyze(records); apply_destination_names(records)
    db = Database(tmp_path / "resume-audit.sqlite")
    scan_id = db.create_scan(src); db.save_scan(scan_id, records, groups, 0, [ignored])
    dest = make_process_folder(parent)
    job = db.create_job(scan_id, src, dest, records); db.set_job(job, "INTERRUPTED", "test")
    resumed = db.resume_job()
    assert resumed["ignored_files"] == [(str(src / ".DS_Store"), ".DS_Store", ".DS_Store", 4, 123, "macOS Finder metadata")]
    db.close()
