from pathlib import Path
from duplicate_file_organizer.models import FileRecord
from duplicate_file_organizer.naming import apply_destination_names
from duplicate_file_organizer.processor import lightweight_destination_check, discover_destination_duplicates, delete_destination_duplicates, write_delete_reports
from duplicate_file_organizer.metadata import extract_creation_metadata

def rec(fid,p):
    return FileRecord(fid,p,p.name,p.name,p.suffix,p.stat().st_size,p.stat().st_mtime_ns,classification='UNIQUE')

def test_lightweight_destination_check_fails_when_any_expected_file_is_missing(tmp_path):
    src=tmp_path/'src';src.mkdir();dest=tmp_path/'dest';dest.mkdir();a=src/'a.txt';b=src/'b.txt';a.write_text('a');b.write_text('bb')
    rs=[rec(1,a),rec(2,b)];apply_destination_names(rs);(dest/rs[0].planned_relative_destination).write_text('a');rs[0].processing_status='COPIED';rs[1].processing_status='FAILED'
    r=lightweight_destination_check(rs,dest)
    assert r['status']=='FAIL' and r['actual_count']==1 and r['expected_count']==2

def test_lightweight_destination_check_detects_missing(tmp_path):
    src=tmp_path/'src';src.mkdir();dest=tmp_path/'dest';dest.mkdir();p=src/'a.txt';p.write_text('a');r=rec(1,p);apply_destination_names([r]);r.processing_status='COPIED'
    r2=lightweight_destination_check([r],dest)
    assert r2['status']=='FAIL' and r2['actual_count']==0

def test_destination_duplicate_delete_only(tmp_path):
    dest=tmp_path/'dest';dest.mkdir();a=dest/'a.bin';b=dest/'b.bin';u=dest/'unique.bin';a.write_bytes(b'same');b.write_bytes(b'same');u.write_bytes(b'other')
    groups=discover_destination_duplicates(dest);assert len(groups)==1 and len(groups[0])==2
    result=delete_destination_duplicates(dest,groups);assert result['deleted']==1 and a.exists() and not b.exists() and u.exists()
    paths=write_delete_reports(dest,groups,result);assert all(p.exists() for p in paths)

def test_delete_does_not_touch_file_outside_destination(tmp_path):
    dest=tmp_path/'dest';dest.mkdir();outside=tmp_path/'outside.bin';outside.write_bytes(b'same');a=dest/'a.bin';b=dest/'b.bin';a.write_bytes(b'same');b.write_bytes(b'same')
    groups=discover_destination_duplicates(dest);result=delete_destination_duplicates(dest,groups);assert outside.exists() and result['deleted']==1

def test_year_location_naming_is_nested(tmp_path):
    p=tmp_path/'photo.jpg';p.write_bytes(b'x');r=rec(1,p);r.creation_year='2024';r.creation_location='GPS_17.70000_83.30000';r.classification='UNIQUE';apply_destination_names([r],'YEAR_LOCATION');assert r.planned_relative_destination.startswith('2024/GPS_17.70000_83.30000/')

def test_metadata_fallback_is_safe(tmp_path):
    p=tmp_path/'plain.txt';p.write_text('x');year,loc,src,date_source,confidence=extract_creation_metadata(p);assert year and loc=='Unknown Location' and src=='NONE' and date_source in {'FILESYSTEM_BIRTHTIME','FILESYSTEM_MODIFIED'} and confidence=='LOW'

def test_lightweight_check_does_not_hide_copy_failures(tmp_path):
    src = tmp_path / 'src'; src.mkdir()
    dest = tmp_path / 'dest'; dest.mkdir()
    a = src / 'a.txt'; b = src / 'b.txt'
    a.write_text('a'); b.write_text('bb')
    records = [rec(1, a), rec(2, b)]
    apply_destination_names(records)
    (dest / records[0].planned_relative_destination).write_text('a')
    records[0].processing_status = 'COPIED'
    records[1].processing_status = 'FAILED'
    result = lightweight_destination_check(records, dest, [records[0]])
    assert result['status'] == 'FAIL'
    assert result['expected_count'] == 2
    assert result['expected_size'] == 3


def test_year_location_end_to_end_copy_path(tmp_path):
    src = tmp_path / 'src'; src.mkdir()
    dest_parent = tmp_path / 'dest_parent'; dest_parent.mkdir()
    p = src / 'photo.jpg'; p.write_bytes(b'photo')
    r = rec(1, p)
    r.creation_year = '2024'
    r.creation_location = 'GPS_17.70000_83.30000'
    apply_destination_names([r], 'YEAR_LOCATION')
    assert r.planned_relative_destination == '2024/GPS_17.70000_83.30000/U000001_UNIQUE_photo.jpg'

def test_scanner_default_includes_os_metadata_files(tmp_path):
    (tmp_path / '.DS_Store').write_bytes(b'metadata')
    scanner = __import__('duplicate_file_organizer.scanner', fromlist=['Scanner']).Scanner(tmp_path)
    records = scanner.discover()
    assert [r.original_filename for r in records] == ['.DS_Store']
    assert scanner.ignored_files == []


def test_office_created_date(tmp_path):
    from zipfile import ZipFile, ZIP_DEFLATED
    p=tmp_path/'old.docx'
    core='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dcterms="http://purl.org/dc/terms/"><dcterms:created>2018-04-10T14:20:00Z</dcterms:created></cp:coreProperties>'
    with ZipFile(p,'w',ZIP_DEFLATED) as z:z.writestr('docProps/core.xml',core)
    year,loc,ls,ds,conf=extract_creation_metadata(p)
    assert (year,ds,conf)==('2018','OFFICE_CORE_CREATED','HIGH')


def test_image_embedded_date_does_not_use_current_filesystem_year(tmp_path):
    from PIL import Image
    p=tmp_path/'photo.jpg'; img=Image.new('RGB',(10,10)); exif=img.getexif(); exif[36867]='2019:07:11 12:30:00'; img.save(p,exif=exif)
    year,loc,ls,ds,conf=extract_creation_metadata(p)
    assert year=='2019' and ds=='EXIF_DateTimeOriginal' and conf=='HIGH'
