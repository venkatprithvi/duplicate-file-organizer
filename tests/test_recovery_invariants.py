from pathlib import Path
from duplicate_file_organizer.models import FileRecord
from duplicate_file_organizer.naming import apply_destination_names

def test_count_and_size_population_invariant():
    records=[FileRecord(1,Path("a"),"a","a","",10,1,classification="UNIQUE"),FileRecord(2,Path("b"),"b","b","",20,1,classification="UNIQUE")]
    apply_destination_names(records)
    assert len(records)==2
    assert sum(r.size_bytes for r in records)==30
    assert len({r.planned_relative_destination.casefold() for r in records})==2

def test_all_records_are_processing_eligible():
    record=FileRecord(1,Path(".DS_Store"),".DS_Store",".DS_Store","",5,1,classification="UNIQUE",metadata_reason="macOS Finder metadata")
    assert record.processing_eligible is True
