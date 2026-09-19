from pathlib import Path
from duplicate_file_organizer.models import FileRecord
from duplicate_file_organizer.naming import apply_destination_names
from duplicate_file_organizer.processor import pre_validate, post_validate, make_process_folder, ValidationError

def record_for(src, name="a.txt"):
    p=src/name
    return FileRecord(1,p,name,name,p.suffix,p.stat().st_size,p.stat().st_mtime_ns,classification="UNIQUE")

def test_pre_and_post_validation(tmp_path):
    src=tmp_path/"src"; src.mkdir(); (src/"a.txt").write_text("abc")
    parent=tmp_path/"dest"; parent.mkdir()
    r=record_for(src); apply_destination_names([r])
    pre=pre_validate(src,[r],parent); assert pre["status"]=="PASS" and pre["count"]==1 and pre["total_size"]==3
    dest=make_process_folder(parent); target=dest/r.planned_relative_destination; target.write_text("abc")
    post=post_validate([r],dest); assert post["status"]=="PASS" and post["actual_count"]==1 and post["actual_size"]==3

def test_pre_validation_rejects_changed_file(tmp_path):
    src=tmp_path/"src"; src.mkdir(); p=src/"a.txt"; p.write_text("abc")
    r=record_for(src); apply_destination_names([r]); p.write_text("changed")
    parent=tmp_path/"dest"; parent.mkdir()
    try: pre_validate(src,[r],parent); assert False
    except ValidationError: assert True

def test_overlap_rejected(tmp_path):
    src=tmp_path/"src"; src.mkdir(); (src/"a.txt").write_text("abc"); parent=src/"inside"; parent.mkdir()
    r=record_for(src); apply_destination_names([r])
    try: pre_validate(src,[r],parent); assert False
    except ValidationError: assert True
