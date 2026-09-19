from pathlib import Path

from duplicate_file_organizer.database import Database
from duplicate_file_organizer.logging_utils import log_path, configure_detailed
from duplicate_file_organizer.models import FileRecord
from duplicate_file_organizer.naming import apply_destination_names
from duplicate_file_organizer.processor import copy_one, make_process_folder, write_review_csv, write_review_summary, write_reports


def _record(src, fid=1):
    r=FileRecord(fid,Path(src),Path(src).name,Path(src).name,Path(src).suffix.lower(),Path(src).stat().st_size,Path(src).stat().st_mtime_ns)
    r.classification="UNIQUE"; return r


def test_copy_one_marks_failure_without_losing_error(tmp_path):
    src=tmp_path/"missing.txt"; dest_parent=tmp_path/"out"; dest_parent.mkdir()
    # record points to a file that doesn't exist: copy_one must record the error and raise
    r=FileRecord(1,src,"missing.txt","missing.txt",".txt",10,1,planned_relative_destination="U000001_UNIQUE_missing.txt")
    db=Database(tmp_path/"db.sqlite"); scan=db.create_scan(tmp_path); db.save_scan(scan,[r],[],0); dest=make_process_folder(dest_parent); job=db.create_job(scan,tmp_path,dest,[r])
    try:
        copy_one(r,dest,db,job,lambda:False)
    except Exception:
        pass
    else:
        raise AssertionError("copy_one should fail for missing source")
    assert r.processing_status == "FAILED"
    assert r.error_message
    row=db.connection.execute("SELECT status,error_message FROM processing_files WHERE job_id=? AND file_id=1",(job,)).fetchone()
    assert row[0] == "FAILED" and row[1]
    db.close()


def test_review_and_final_reports_are_created(tmp_path):
    src=tmp_path/"src"; src.mkdir(); (src/"a.txt").write_text("abc")
    r=_record(src/"a.txt"); apply_destination_names([r])
    review=write_review_csv([r],src,[],tmp_path/"review",7)
    assert review.exists() and "planned_destination" in review.read_text(encoding="utf-8-sig")
    dest=make_process_folder(tmp_path)
    pre={"status":"SKIPPED BY USER"}; post={"status":"SKIPPED BY USER"}
    paths=write_reports([r],src,dest,pre,post,[],[],validation_enabled=False,pre_validation_enabled=False,detailed_logging=False)
    assert paths[0].exists() and paths[1].exists() and paths[2].exists() and paths[3] is None
    summary=paths[2].read_text(encoding="utf-8-sig")
    assert "Detailed logging,DISABLED" in summary
    assert "Pre-validation,SKIPPED BY USER" in summary
    assert "Post-validation,SKIPPED BY USER" in summary


def test_detailed_logging_flag_is_configurable():
    logger=configure_detailed(False)
    assert logger._dfo_detailed is False
    logger=configure_detailed(True)
    assert logger._dfo_detailed is True
    assert log_path().parent.exists()


def test_job_persists_processing_options_for_resume(tmp_path):
    db=Database(tmp_path/"db.sqlite"); src=tmp_path/"src"; out=tmp_path/"out"; src.mkdir(); out.mkdir()
    p=src/"a.txt"; p.write_text("a")
    r=_record(p); r.planned_relative_destination="U000001_UNIQUE_a.txt"
    scan=db.create_scan(src); db.save_scan(scan,[r],[],0); dest=make_process_folder(out)
    import json
    job=db.create_job(scan,src,dest,[r],options_json=json.dumps({"detailed_logging":False,"pre_validation":True,"post_validation":False}))
    db.set_job(job,"PROCESSING")
    resumed=db.resume_job()
    assert json.loads(resumed["options_json"]) == {"detailed_logging":False,"pre_validation":True,"post_validation":False}
    db.close()


def test_copy_status_contains_timing_and_hash_columns(tmp_path):
    src=tmp_path/"a.txt"; src.write_text("abc")
    dest=make_process_folder(tmp_path)
    r=_record(src); apply_destination_names([r])
    db=Database(tmp_path/"db.sqlite"); scan=db.create_scan(tmp_path); db.save_scan(scan,[r],[],0); job=db.create_job(scan,tmp_path,dest,[r])
    copy_one(r,dest,db,job,lambda:False)
    paths=write_reports([r],tmp_path,dest,{"status":"SKIPPED BY USER"},{"status":"SKIPPED BY USER"},[],[],validation_enabled=False,pre_validation_enabled=False,detailed_logging=False)
    text=paths[0].read_text(encoding="utf-8-sig")
    assert "copy_start_utc" in text and "copy_end_utc" in text and "duration_seconds" in text
    assert "source_sha256" in text and "destination_sha256" in text
    assert r.copy_started_at and r.copy_completed_at and r.copy_duration_seconds is not None
    db.close()

def test_review_summary_is_available_before_processing(tmp_path):
    src=tmp_path/"a.txt"; src.write_text("abc")
    r=_record(src); apply_destination_names([r])
    path=write_review_summary([r],[],tmp_path/"reports",11)
    text=path.read_text(encoding="utf-8-sig")
    assert "Files to process" in text and "Total bytes to copy" in text and "Unique" in text
