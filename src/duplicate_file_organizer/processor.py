from __future__ import annotations
import csv,json,os,shutil,tempfile,time,logging
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
from typing import Callable
from .hashing import sha256_file
from .models import FileRecord
from .naming import collision_key
from .logging_utils import configure_detailed,info,essential

class ValidationError(Exception): pass
class ProcessingCancelled(Exception): pass

def normalized_extension(filename):
    name=Path(filename).name
    if name.startswith('.') and name.count('.')==1:return '<NO_EXTENSION>'
    return (Path(name).suffix or '<NO_EXTENSION>').casefold()
def extension_counts(records): return Counter(normalized_extension(r.original_filename) for r in records)
def _destination_original_name(name):
    n=Path(name).name
    parts=n.split('_',3)
    if len(parts)==4 and parts[0].startswith('G') and parts[1].isdigit() and parts[2] in ('ORIGINAL','DUPLICATE'): return parts[3]
    parts=n.split('_',2)
    if len(parts)==3 and parts[0].startswith('U') and parts[1]=='UNIQUE': return parts[2]
    return n
def is_within(path,parent):
    try: Path(path).resolve(strict=False).relative_to(Path(parent).resolve(strict=False)); return True
    except ValueError:return False

def validate_parent(source,parent,required_bytes=0):
    source=Path(source).resolve(strict=False); parent=Path(parent).resolve(strict=False)
    if not source.is_dir():raise ValidationError('Source folder does not exist or is not a directory.')
    if not parent.exists() or not parent.is_dir():raise ValidationError('Destination parent does not exist or is not a directory.')
    if source==parent or is_within(parent,source) or is_within(source,parent):raise ValidationError('Source and destination parent cannot be the same folder or nested inside each other.')
    if not os.access(parent,os.W_OK):raise ValidationError('Destination parent is not writable.')
    free=shutil.disk_usage(parent).free
    if free<max(0,required_bytes):raise ValidationError(f'Insufficient disk space. Required {required_bytes:,}; available {free:,} bytes.')

def _source_snapshot_validate(record,capture_hash=True):
    errors=[]
    try:
        if not record.source_path.exists() or not record.source_path.is_file():return [f'Source file is missing or is not a regular file: {record.source_path}']
        st=record.source_path.stat()
        if st.st_size!=record.size_bytes:errors.append(f'Size changed since scan: {record.source_path}')
        if st.st_mtime_ns!=record.modified_ns:errors.append(f'Modification time changed since scan: {record.source_path}')
        with record.source_path.open('rb') as f:f.read(1)
        if capture_hash and not errors:record.prevalidated_hash=sha256_file(record.source_path)
    except OSError as e:errors.append(f'Unreadable/unavailable: {record.source_path} ({e})')
    return errors

def pre_validate(source,records,parent,*,required_bytes=None):
    expected_size=sum(r.size_bytes for r in records); validate_parent(source,parent,expected_size if required_bytes is None else required_bytes)
    errors=[]; seen=set()
    for r in records:
        if not r.planned_relative_destination:errors.append(f'Missing destination mapping: {r.source_path}');continue
        key=collision_key(r.planned_relative_destination)
        if key in seen:errors.append(f'Duplicate destination mapping: {r.planned_relative_destination}')
        seen.add(key); errors.extend(_source_snapshot_validate(r,True))
    if errors:raise ValidationError('\n'.join(errors[:50])+((f'\n...and {len(errors)-50} more.') if len(errors)>50 else ''))
    return {'status':'PASS','count':len(records),'total_size':expected_size,'extension_counts':dict(extension_counts(records))}

def make_process_folder(parent):
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S'); c=Path(parent)/f'DuplicateReview_{stamp}'; n=2
    while c.exists():c=Path(parent)/f'DuplicateReview_{stamp}_{n}';n+=1
    c.mkdir(parents=True);return c

def _safe_temp_target(target):
    fd,raw=tempfile.mkstemp(prefix=f'.{target.name}.',suffix='.dfo-partial',dir=target.parent);os.close(fd);Path(raw).unlink(missing_ok=True);return Path(raw)

def copy_one(record,destination,db,job_id,cancelled,logger=None):
    if cancelled and cancelled():raise ProcessingCancelled()
    target=Path(destination)/record.planned_relative_destination;target.parent.mkdir(parents=True,exist_ok=True);started=datetime.now(timezone.utc);record.copy_started_at=started.isoformat();db.set_file(job_id,record.file_id,'COPYING',record.prevalidated_hash)
    tmp=None
    try:
        tmp=_safe_temp_target(target);shutil.copy2(record.source_path,tmp)
        if tmp.stat().st_size!=record.size_bytes:raise ValidationError(f'Copied size mismatch: {target}')
        os.replace(tmp,target);tmp=None;record.processing_status='COPIED';record.copy_completed_at=datetime.now(timezone.utc).isoformat();record.copy_duration_seconds=(datetime.fromisoformat(record.copy_completed_at)-started).total_seconds();db.set_file(job_id,record.file_id,'COPIED',record.prevalidated_hash)
        if logger:info(logger,f'COPY SUCCESS | file_id={record.file_id} | bytes={record.size_bytes} | seconds={record.copy_duration_seconds:.3f}')
        return target
    except Exception as exc:
        if tmp:tmp.unlink(missing_ok=True)
        record.copy_completed_at=datetime.now(timezone.utc).isoformat();record.copy_duration_seconds=(datetime.fromisoformat(record.copy_completed_at)-started).total_seconds();record.processing_status='FAILED';record.error_message=f'{type(exc).__name__}: {exc}';db.set_file(job_id,record.file_id,'FAILED',record.prevalidated_hash,error=record.error_message)
        if logger:essential(logger,logging.ERROR,f'COPY FAILED | file_id={record.file_id} | source={record.source_path} | error={record.error_message}')
        raise

def _iter_destination_files(destination):
    root=Path(destination);reports=root/'reports'
    for p in root.rglob('*'):
        if p.is_file() and reports not in p.parents and not p.name.endswith('.dfo-partial'):yield p

def lightweight_destination_check(records, destination, successful_records=None):
    """Reconcile destination against the complete expected source population.

    ``successful_records`` is retained for API compatibility but is deliberately
    ignored: a failed copy must produce a FAIL result rather than reducing the
    expected count.
    """
    expected = {
        collision_key(r.planned_relative_destination): r
        for r in records
        if r.planned_relative_destination
    }
    actual_paths = list(_iter_destination_files(destination))
    actual = {
        collision_key(p.relative_to(destination).as_posix()): p
        for p in actual_paths
    }
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    size = sum(p.stat().st_size for p in actual.values())
    expected_size = sum(r.size_bytes for r in records)
    return {
        'status': 'PASS' if not missing and not extra and len(actual) == len(expected) and size == expected_size else 'FAIL',
        'expected_count': len(expected),
        'actual_count': len(actual),
        'expected_size': expected_size,
        'actual_size': size,
        'missing': missing,
        'unexpected': extra,
    }

def post_validate(records,destination):
    expected={collision_key(r.planned_relative_destination):r for r in records if r.planned_relative_destination}
    actual_paths=list(_iter_destination_files(destination));actual={collision_key(p.relative_to(destination).as_posix()):p for p in actual_paths};missing=sorted(set(expected)-set(actual));unexpected=sorted(set(actual)-set(expected));size_mismatches=[];name_mismatches=[]
    for key,r in expected.items():
        if key in actual:
            p=actual[key]
            if p.stat().st_size!=r.size_bytes:size_mismatches.append(f'{p} expected {r.size_bytes} actual {p.stat().st_size}')
            if p.name!=Path(r.planned_relative_destination).name:name_mismatches.append(f'{p} expected {r.planned_relative_destination}')
    expected_size=sum(r.size_bytes for r in expected.values());actual_size=sum(p.stat().st_size for p in actual.values());ext_ok=Counter(normalized_extension(r.original_filename) for r in expected.values())==Counter(normalized_extension(_destination_original_name(p.name)) for p in actual.values())
    ok=not missing and not unexpected and not size_mismatches and ext_ok and len(expected)==len(actual) and expected_size==actual_size
    return {'status':'PASS' if ok else 'FAIL','expected_count':len(expected),'actual_count':len(actual),'expected_size':expected_size,'actual_size':actual_size,'count_ok':len(expected)==len(actual),'size_ok':expected_size==actual_size,'extension_ok':ext_ok,'extension_mismatches':name_mismatches,'missing':missing,'unexpected':unexpected,'size_mismatches':size_mismatches}

def resume_required_bytes(records,destination):
    total=0
    for r in records:
        t=Path(destination)/r.planned_relative_destination
        if t.is_file() and t.stat().st_size==r.size_bytes and r.prevalidated_hash:
            try:
                if sha256_file(t)==r.prevalidated_hash:continue
            except OSError:pass
        total+=r.size_bytes
    return total

def verify_hashes(records,destination,db,job_id,cancelled,progress=None,logger=None):
    failures=[];verified=0
    for i,r in enumerate(records,1):
        if cancelled and cancelled():raise ProcessingCancelled()
        if r.processing_status=='FAILED':
            if progress:progress(i,len(records),str(r.source_path));continue
        t=Path(destination)/r.planned_relative_destination
        try:
            sh=r.prevalidated_hash or sha256_file(r.source_path);dh=sha256_file(t);r.prevalidated_hash=sh;r.destination_hash=dh
            if sh!=dh:raise ValueError('HASH_MISMATCH')
            r.processing_status='VERIFIED';r.error_message=None;db.set_file(job_id,r.file_id,'VERIFIED',sh,dh);verified+=1
        except Exception as exc:
            r.processing_status='FAILED';r.error_message=f'{type(exc).__name__}: {exc}';failures.append((str(r.source_path),str(t),r.error_message));db.set_file(job_id,r.file_id,'FAILED',r.prevalidated_hash,error=r.error_message)
        if progress:progress(i,len(records),str(r.source_path))
    return verified,failures

def _write_csv(path,header,rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',newline='',encoding='utf-8-sig') as f:csv.writer(f).writerows([header,*rows])

def write_review_csv(records,source,ignored_files=None,output_dir=None,scan_id=None):
    from .app_paths import app_data_dir
    d=Path(output_dir or app_data_dir()/'reports');p=d/f'review_{scan_id or datetime.now().strftime("%Y%m%d_%H%M%S")}.csv';rows=[]
    for r in records:rows.append([r.file_id,r.classification,r.group_id or '',r.sequence or '',r.original_filename,str(r.source_path),r.relative_path,r.size_bytes,r.extension,r.sha256 or '',r.creation_year or '',r.creation_location or '',r.location_source or '',r.creation_date_source or '',r.creation_date_confidence or '',r.planned_relative_destination or '','TO_PROCESS'])
    for i in ignored_files or []:rows.append(['','IGNORED_SYSTEM_METADATA','','',i.filename,str(i.source_path),i.relative_path,i.size_bytes,'','','','','','','IGNORED'])
    _write_csv(p,['file_id','classification','group','sequence','original_filename','source_path','relative_path','size_bytes','extension','sha256','creation_year','creation_location','location_source','creation_date_source','creation_date_confidence','planned_destination','review_status'],rows);return p

def write_review_summary(records,ignored_files=None,output_dir=None,scan_id=None,organization='FLAT'):
    from .app_paths import app_data_dir
    d=Path(output_dir or app_data_dir()/'reports');p=d/f'review_summary_{scan_id or datetime.now().strftime("%Y%m%d_%H%M%S")}.csv';ignored_files=ignored_files or []
    rows=[('REVIEW','Files to process',len(records)),('REVIEW','Ignored system metadata',len(ignored_files)),('REVIEW','Duplicate groups',len({r.group_id for r in records if r.group_id})),('REVIEW','Original/representative files',sum(r.classification=='ORIGINAL' for r in records)),('REVIEW','Duplicates',sum(r.classification=='DUPLICATE' for r in records)),('REVIEW','Unique',sum(r.classification=='UNIQUE' for r in records)),('REVIEW','Total bytes to copy',sum(r.size_bytes for r in records)),('OPTIONS','Destination organization',organization)]
    _write_csv(p,['phase','metric','value'],rows);return p

def write_reports(records,source,destination,pre,post,failures,ignored_files=None,*,validation_enabled=True,pre_validation_enabled=True,detailed_logging=True,lightweight_check=None,organization='FLAT',started_at=None,completed_at=None):
    reports=Path(destination)/'reports';reports.mkdir(exist_ok=True);ignored_files=ignored_files or []
    copy_path=reports/'copy_status.csv'; rows=[]
    for r in records:
        hs='VERIFIED' if r.destination_hash and r.prevalidated_hash==r.destination_hash else ('NOT_RUN' if not validation_enabled else 'FAILED')
        rows.append([r.file_id,r.classification,r.group_id or '',r.sequence or '',r.original_filename,str(r.source_path),r.planned_relative_destination or '',r.size_bytes,r.copy_started_at or '',r.copy_completed_at or '',f'{r.copy_duration_seconds:.3f}' if r.copy_duration_seconds is not None else '',r.processing_status,r.error_message or '',r.prevalidated_hash or '',r.destination_hash or '',hs])
    _write_csv(copy_path,['file_id','classification','group','sequence','original_filename','source_path','destination','size_bytes','copy_start_utc','copy_end_utc','duration_seconds','status','error','source_sha256','destination_sha256','hash_verification'],rows)
    validation_path=reports/'validation_report.csv' if validation_enabled else None
    if validation_path:
        vr=[('LIGHTWEIGHT_DESTINATION_COUNT','PASS' if lightweight_check and lightweight_check.get('status')=='PASS' else 'FAIL' if lightweight_check else 'UNKNOWN',json.dumps(lightweight_check or {})),('COUNT','PASS' if post.get('count_ok') else 'FAIL',str(post.get('actual_count'))),('SIZE','PASS' if post.get('size_ok') else 'FAIL',f"{post.get('actual_size')} vs {post.get('expected_size')}"),('EXTENSION','PASS' if post.get('extension_ok') else 'FAIL',''),('MISSING','PASS' if not post.get('missing') else 'FAIL',' | '.join(post.get('missing',[]))),('UNEXPECTED','PASS' if not post.get('unexpected') else 'FAIL',' | '.join(post.get('unexpected',[]))),('SIZE_MISMATCH','PASS' if not post.get('size_mismatches') else 'FAIL',' | '.join(post.get('size_mismatches',[]))),('SHA256','PASS' if not failures else 'FAIL',' | '.join(map(str,failures)))]
        _write_csv(validation_path,['check','status','details'],vr)
    summary_path=reports/'process_summary.csv';successful=sum(r.processing_status in ('COPIED','VERIFIED') for r in records);failed=sum(r.processing_status=='FAILED' for r in records);rows=[('SCAN','Files to process',len(records)),('SCAN','Ignored system metadata',len(ignored_files)),('REVIEW','Duplicate groups',len({r.group_id for r in records if r.group_id})),('REVIEW','Original/representative files',sum(r.classification=='ORIGINAL' for r in records)),('REVIEW','Duplicates',sum(r.classification=='DUPLICATE' for r in records)),('REVIEW','Unique',sum(r.classification=='UNIQUE' for r in records)),('OPTIONS','Destination organization',organization),('OPTIONS','Detailed logging','ENABLED' if detailed_logging else 'DISABLED'),('OPTIONS','Pre-validation','ENABLED' if pre_validation_enabled else 'SKIPPED BY USER'),('OPTIONS','Post-validation + SHA-256','ENABLED' if validation_enabled else 'SKIPPED BY USER'),('COPY','Expected files',len(records)),('COPY','Successful files',successful),('COPY','Failed files',failed),('COPY','Expected bytes',sum(r.size_bytes for r in records)),('COPY','Copied bytes',sum(r.size_bytes for r in records if r.processing_status in ('COPIED','VERIFIED'))),('COPY','Lightweight destination check',lightweight_check.get('status') if lightweight_check else 'UNKNOWN'),('VALIDATION','Post-validation',post.get('status') if validation_enabled else 'SKIPPED BY USER'),('VALIDATION','SHA-256 failures',len(failures) if validation_enabled else 'SKIPPED BY USER')];_write_csv(summary_path,['phase','metric','value'],rows)
    manifest=reports/'process_manifest.json';manifest.write_text(json.dumps({'version':'1.0-upgraded','source':str(source),'destination':str(destination),'organization':organization,'options':{'detailed_logging':detailed_logging,'pre_validation':pre_validation_enabled,'post_validation':validation_enabled},'lightweight_destination_check':lightweight_check,'pre_validation':pre,'post_validation':post,'copy_failures':[{'source':str(r.source_path),'error':r.error_message} for r in records if r.processing_status=='FAILED'],'ignored_system_metadata':[{'source_path':str(i.source_path),'relative_path':i.relative_path,'reason':i.reason} for i in ignored_files],'copy_only':True},indent=2,ensure_ascii=False),encoding='utf-8')
    return copy_path,manifest,summary_path,validation_path

def discover_destination_duplicates(destination,progress=None,cancelled=None):
    files=list(_iter_destination_files(Path(destination))); by_size={}
    for p in files:by_size.setdefault(p.stat().st_size,[]).append(p)
    groups=[]
    for candidates in by_size.values():
        if len(candidates)<2:continue
        by_hash={}
        for p in candidates:
            if cancelled and cancelled():raise ProcessingCancelled()
            try: h=sha256_file(p);by_hash.setdefault(h,[]).append(p)
            except OSError:continue
        groups.extend(g for g in by_hash.values() if len(g)>1)
    groups.sort(key=lambda g:str(min(g)))
    return groups

def delete_destination_duplicates(destination,groups,*,cancelled=None,logger=None):
    """Delete only duplicate members inside destination. First file in each group is retained."""
    root=Path(destination).resolve();status=[];deleted=0;failed=0;bytes_deleted=0
    for group in groups:
        group=sorted(group,key=lambda p:(str(p).casefold(),str(p))); keep=group[0];keep_hash=sha256_file(keep)
        for p in group[1:]:
            if cancelled and cancelled():raise ProcessingCancelled()
            try:
                if p.resolve() == root or not is_within(p,root):raise ValidationError('Unsafe deletion target outside destination.')
                if sha256_file(p)!=keep_hash:raise ValidationError('Hash changed before deletion.')
                size=p.stat().st_size;p.unlink();status.append([str(p.relative_to(root)),str(keep.relative_to(root)),size,'DELETED','']);deleted+=1;bytes_deleted+=size
            except Exception as exc:
                status.append([str(p.relative_to(root)),str(keep.relative_to(root)),p.stat().st_size if p.exists() else '', 'FAILED',f'{type(exc).__name__}: {exc}']);failed+=1
                if logger:essential(logger,logging.ERROR,f'DELETE FAILED | {p} | {exc}')
    return {'deleted':deleted,'failed':failed,'bytes_deleted':bytes_deleted,'status':status}

def write_delete_reports(destination, groups, result, review_rows=None):
    reports=Path(destination)/'reports'; reports.mkdir(exist_ok=True)
    review=reports/'delete_review.csv'
    rows=review_rows or []
    if not rows:
        for g in groups:
            g=sorted(g,key=lambda p:(str(p).casefold(),str(p))); keep=g[0]
            try: h=sha256_file(keep)
            except OSError: h=''
            for seq,p in enumerate(g,1):
                size=p.stat().st_size if p.exists() else ''
                rows.append([seq,'KEEP' if p==keep else 'DELETE_CANDIDATE',str(p.relative_to(destination)),size,h])
    _write_csv(review,['sequence','action','destination_relative_path','size_bytes','group_sha256'],rows)
    status=reports/'delete_status.csv'; _write_csv(status,['file','kept_representative','size_bytes','status','error'],result['status'])
    summary=reports/'delete_summary.csv'; _write_csv(summary,['metric','value'],[('Duplicate groups',len(groups)),('Deleted files',result['deleted']),('Failed deletions',result['failed']),('Bytes recovered',result['bytes_deleted'])])
    return review,status,summary
