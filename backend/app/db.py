import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager
from uuid import uuid4
import json

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    c=sqlite3.connect(str(path),timeout=10,check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=10000")
    return c
@contextmanager
def session(path):
    c=connect(path)
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()
def initialize(path):
    with session(path) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)")
        for migration in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
            version = int(migration.name.split("_", 1)[0])
            applied = c.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone()
            if applied:
                continue
            c.executescript(migration.read_text(encoding="utf-8"))
            c.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)", (version, now()))
        c.execute("INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES('global_paused','false',?)",(now(),))
        c.execute("INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES('operating_mode','supervised',?)",(now(),))
def event(c, level, kind, message, details=None):
    c.execute("INSERT INTO system_events(id,level,event_type,message,details_json,created_at) VALUES(?,?,?,?,?,?)",
      (str(uuid4()),level,kind,message,json.dumps(details or {}),now()))

def recover_interrupted_jobs(path):
    """Recover interrupted work without blindly retrying uploads with uncertain remote outcomes."""
    t=now()
    with session(path) as c:
        uncertain=list(c.execute("SELECT id,payload_json FROM jobs WHERE status='RUNNING' AND job_type='UPLOAD_YOUTUBE'"))
        for job in uncertain:
            try:
                payload=json.loads(job["payload_json"] or "{}")
                published_id=payload.get("published_video_id")
                if published_id:
                    c.execute("UPDATE published_videos SET publishing_status='RECONCILE_REQUIRED',updated_at=? WHERE id=?",(t,published_id))
            except (ValueError,TypeError):
                pass
            c.execute("UPDATE jobs SET status='RECONCILE_REQUIRED',error='Application restarted while upload was in progress; reconcile remote status before retrying.',updated_at=?,finished_at=? WHERE id=?",(t,t,job["id"]))
            event(c,"ERROR","job.recovery_reconciliation_required","Interrupted YouTube upload requires manual reconciliation.",{"job_id":job["id"]})
        interrupted=c.execute("SELECT id FROM jobs WHERE status='RUNNING' AND job_type<>'UPLOAD_YOUTUBE'").fetchall()
        for job in interrupted:
            c.execute("UPDATE jobs SET status='QUEUED',error='Recovered after application restart.',updated_at=?,next_attempt_at=? WHERE id=?",(t,t,job["id"]))
            event(c,"WARNING","job.recovered","Interrupted job returned to queue after application restart.",{"job_id":job["id"]})
