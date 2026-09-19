from duplicate_file_organizer.database import Database

def test_database_scan_and_resume(tmp_path):
    db=Database(tmp_path/"inventory.sqlite")
    scan_id=db.create_scan(tmp_path)
    assert scan_id==1
    assert db.resume_job() is None
    db.close()
