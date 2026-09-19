from __future__ import annotations
import json, sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA='''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS scan_sessions(scan_id INTEGER PRIMARY KEY AUTOINCREMENT,source_path TEXT NOT NULL,started_at TEXT NOT NULL,completed_at TEXT,status TEXT,total_files INTEGER DEFAULT 0,total_size_bytes INTEGER DEFAULT 0,duplicate_group_count INTEGER DEFAULT 0,scan_error_count INTEGER DEFAULT 0,ignored_file_count INTEGER DEFAULT 0,ignored_size_bytes INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS ignored_files(scan_id INTEGER NOT NULL,source_path TEXT NOT NULL,relative_path TEXT NOT NULL,filename TEXT NOT NULL,size_bytes INTEGER NOT NULL,modified_ns INTEGER NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(scan_id,relative_path),FOREIGN KEY(scan_id) REFERENCES scan_sessions(scan_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS files(file_id INTEGER NOT NULL,scan_id INTEGER NOT NULL,source_path TEXT NOT NULL,relative_path TEXT NOT NULL,original_filename TEXT NOT NULL,extension TEXT NOT NULL,size_bytes INTEGER NOT NULL,modified_ns INTEGER NOT NULL,partial_hash TEXT,sha256 TEXT,classification TEXT NOT NULL,group_id INTEGER,sequence INTEGER,metadata_reason TEXT,planned_relative_destination TEXT,processing_status TEXT DEFAULT 'PENDING',prevalidated_hash TEXT,destination_hash TEXT,error_message TEXT,creation_year TEXT,creation_location TEXT,location_source TEXT,creation_date_source TEXT,creation_date_confidence TEXT,PRIMARY KEY(scan_id,file_id),FOREIGN KEY(scan_id) REFERENCES scan_sessions(scan_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS processing_jobs(job_id INTEGER PRIMARY KEY AUTOINCREMENT,scan_id INTEGER NOT NULL,source_path TEXT NOT NULL,destination_path TEXT NOT NULL,created_at TEXT NOT NULL,started_at TEXT,completed_at TEXT,status TEXT NOT NULL,expected_count INTEGER NOT NULL,expected_size INTEGER NOT NULL,last_error TEXT,options_json TEXT,FOREIGN KEY(scan_id) REFERENCES scan_sessions(scan_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS processing_files(job_id INTEGER NOT NULL,scan_id INTEGER NOT NULL,file_id INTEGER NOT NULL,destination_path TEXT NOT NULL,status TEXT NOT NULL,source_hash TEXT,destination_hash TEXT,error_message TEXT,PRIMARY KEY(job_id,file_id),FOREIGN KEY(job_id) REFERENCES processing_jobs(job_id) ON DELETE CASCADE,FOREIGN KEY(scan_id,file_id) REFERENCES files(scan_id,file_id) ON DELETE CASCADE);
'''
def now(): return datetime.now(timezone.utc).isoformat()
class Database:
    def __init__(self,path:Path):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True); self.connection=sqlite3.connect(self.path); self.connection.execute('PRAGMA foreign_keys=ON'); self.connection.execute('PRAGMA journal_mode=WAL'); self.connection.execute('PRAGMA busy_timeout=5000'); self.connection.executescript(SCHEMA); self._migrate(); self.connection.commit()
    def _migrate(self):
        cols={r[1] for r in self.connection.execute('PRAGMA table_info(files)')}
        for name in ('creation_year','creation_location','location_source','creation_date_source','creation_date_confidence'):
            if name not in cols: self.connection.execute(f'ALTER TABLE files ADD COLUMN {name} TEXT')
    def close(self): self.connection.close()
    def create_scan(self,source):
        cur=self.connection.execute('INSERT INTO scan_sessions(source_path,started_at,status) VALUES(?,?,?)',(str(source),now(),'SCANNING')); self.connection.commit(); return int(cur.lastrowid)
    def mark_scan_cancelled(self,scan_id):
        with self.connection: self.connection.execute('UPDATE scan_sessions SET completed_at=?,status=? WHERE scan_id=?',(now(),'CANCELLED',scan_id))
    def save_scan(self,scan_id,records,groups,error_count,ignored_files=None):
        with self.connection:
            self.connection.executemany('''INSERT INTO files(file_id,scan_id,source_path,relative_path,original_filename,extension,size_bytes,modified_ns,partial_hash,sha256,classification,group_id,sequence,metadata_reason,planned_relative_destination,processing_status,creation_year,creation_location,location_source,creation_date_source,creation_date_confidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',[(r.file_id,scan_id,str(r.source_path),r.relative_path,r.original_filename,r.extension,r.size_bytes,r.modified_ns,r.partial_hash,r.sha256,r.classification or 'UNIQUE',r.group_id,r.sequence,r.metadata_reason,r.planned_relative_destination,r.processing_status,r.creation_year,r.creation_location,r.location_source,r.creation_date_source,r.creation_date_confidence) for r in records])
            ignored=ignored_files or []
            self.connection.executemany('INSERT INTO ignored_files(scan_id,source_path,relative_path,filename,size_bytes,modified_ns,reason) VALUES(?,?,?,?,?,?,?)',[(scan_id,str(i.source_path),i.relative_path,i.filename,i.size_bytes,i.modified_ns,i.reason) for i in ignored]) if ignored else None
            self.connection.execute('UPDATE scan_sessions SET completed_at=?,status=?,total_files=?,total_size_bytes=?,duplicate_group_count=?,scan_error_count=?,ignored_file_count=?,ignored_size_bytes=? WHERE scan_id=?',(now(),'COMPLETED' if error_count==0 else 'COMPLETED_WITH_ERRORS',len(records),sum(r.size_bytes for r in records),len(groups),error_count,len(ignored),sum(i.size_bytes for i in ignored),scan_id))
    def create_job(self,scan_id,source,destination,records,options_json=None):
        cur=self.connection.execute('INSERT INTO processing_jobs(scan_id,source_path,destination_path,created_at,status,expected_count,expected_size,options_json) VALUES(?,?,?,?,?,?,?,?)',(scan_id,str(source),str(destination),now(),'PRE_VALIDATION',len(records),sum(r.size_bytes for r in records),options_json)); job=int(cur.lastrowid)
        self.connection.executemany('INSERT INTO processing_files(job_id,scan_id,file_id,destination_path,status) VALUES(?,?,?,?,?)',[(job,scan_id,r.file_id,str(destination/r.planned_relative_destination),'PENDING') for r in records]); self.connection.commit(); return job
    def set_job(self,job_id,status,error=None):
        t=now()
        with self.connection: self.connection.execute('''UPDATE processing_jobs SET status=?,last_error=?,started_at=CASE WHEN ?='PROCESSING' AND started_at IS NULL THEN ? ELSE started_at END,completed_at=CASE WHEN ? IN ('COMPLETED','COMPLETED_WITH_ERRORS','FAILED','INTERRUPTED') THEN ? ELSE completed_at END WHERE job_id=?''',(status,error,status,t,status,t,job_id))
    def set_file(self,job_id,file_id,status,source_hash=None,destination_hash=None,error=None):
        row=self.connection.execute('SELECT scan_id FROM processing_jobs WHERE job_id=?',(job_id,)).fetchone()
        if not row: raise KeyError(job_id)
        scan_id=int(row[0])
        with self.connection:
            self.connection.execute('UPDATE processing_files SET status=?,source_hash=?,destination_hash=?,error_message=? WHERE job_id=? AND file_id=?',(status,source_hash,destination_hash,error,job_id,file_id))
            self.connection.execute('UPDATE files SET processing_status=?,prevalidated_hash=?,destination_hash=?,error_message=? WHERE scan_id=? AND file_id=?',(status,source_hash,destination_hash,error,scan_id,file_id))
    def resume_job(self):
        row=self.connection.execute('SELECT job_id,scan_id,source_path,destination_path,status,expected_count,expected_size,options_json FROM processing_jobs WHERE status IN (\'PROCESSING\',\'INTERRUPTED\',\'FAILED\',\'COMPLETED_WITH_ERRORS\') ORDER BY job_id DESC LIMIT 1').fetchone()
        if not row:return None
        job_id,scan_id,source,dest,status,count,size,opts=row
        rows=self.connection.execute('''SELECT f.file_id,f.source_path,f.relative_path,f.original_filename,f.extension,f.size_bytes,f.modified_ns,f.partial_hash,f.sha256,f.classification,f.group_id,f.sequence,f.metadata_reason,f.planned_relative_destination,p.status,p.source_hash,p.destination_hash,p.error_message,f.creation_year,f.creation_location,f.location_source,f.creation_date_source,f.creation_date_confidence FROM processing_files p JOIN files f ON f.scan_id=p.scan_id AND f.file_id=p.file_id WHERE p.job_id=? ORDER BY f.file_id''',(job_id,)).fetchall()
        ignored=self.connection.execute('SELECT source_path,relative_path,filename,size_bytes,modified_ns,reason FROM ignored_files WHERE scan_id=? ORDER BY relative_path',(scan_id,)).fetchall()
        return {'job_id':int(job_id),'scan_id':int(scan_id),'source_path':source,'destination_path':dest,'status':status,'expected_count':count,'expected_size':size,'options_json':opts,'files':rows,'ignored_files':ignored}
