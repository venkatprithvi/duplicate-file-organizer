from pathlib import Path
from duplicate_file_organizer.scanner import Scanner
from duplicate_file_organizer.naming import apply_destination_names, sanitize_filename

def test_scan_ignores_ds_store_but_keeps_user_files(tmp_path):
    (tmp_path / "a.txt").write_text("same")
    (tmp_path / "b.txt").write_text("same")
    (tmp_path / ".DS_Store").write_bytes(b"metadata")
    (tmp_path / ".gitignore").write_text("*.tmp")
    scanner=Scanner(tmp_path, ignore_system_metadata=True)
    records=scanner.discover()
    groups=scanner.analyze(records)
    apply_destination_names(records)
    assert len(records)==3
    assert len(groups)==1
    assert all(r.planned_relative_destination for r in records)
    assert [i.filename for i in scanner.ignored_files] == [".DS_Store"]

def test_exact_duplicate_classification(tmp_path):
    (tmp_path / "old.txt").write_text("same")
    (tmp_path / "new.txt").write_text("same")
    scanner=Scanner(tmp_path); records=scanner.discover(); groups=scanner.analyze(records); apply_destination_names(records)
    assert len(groups)==1
    assert {r.classification for r in records}=={"ORIGINAL","DUPLICATE"}

def test_unique_naming():
    from duplicate_file_organizer.models import FileRecord
    r=FileRecord(10,Path("/tmp/a.pdf"),"a.pdf","a.pdf",".pdf",1,1,classification="UNIQUE")
    apply_destination_names([r])
    assert r.planned_relative_destination=="U000010_UNIQUE_a.pdf"

def test_windows_filename_sanitization():
    assert sanitize_filename("a:b*c?.txt")=="a_b_c_.txt"
    assert sanitize_filename("CON.txt")=="_CON_.txt"


def test_destination_names_are_flat_no_group_or_unique_folders():
    from duplicate_file_organizer.models import FileRecord
    records = [
        FileRecord(1, Path("/tmp/a.pdf"), "a.pdf", "a.pdf", ".pdf", 1, 1, classification="ORIGINAL", group_id=7, sequence=1),
        FileRecord(2, Path("/tmp/b.pdf"), "b.pdf", "b.pdf", ".pdf", 1, 2, classification="DUPLICATE", group_id=7, sequence=2),
        FileRecord(3, Path("/tmp/c.txt"), "c.txt", "c.txt", ".txt", 1, 3, classification="UNIQUE"),
    ]
    apply_destination_names(records)
    assert records[0].planned_relative_destination == "G000007_01_ORIGINAL_a.pdf"
    assert records[1].planned_relative_destination == "G000007_02_DUPLICATE_b.pdf"
    assert records[2].planned_relative_destination == "U000003_UNIQUE_c.txt"
    assert all("/" not in r.planned_relative_destination and "\\" not in r.planned_relative_destination for r in records)


def test_default_scanner_includes_regular_metadata_files(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / ".DS_Store").write_bytes(b"metadata")
    scanner=Scanner(tmp_path)
    records=scanner.discover()
    assert {r.original_filename for r in records} == {"a.txt", ".DS_Store"}
    assert scanner.ignored_files == []
