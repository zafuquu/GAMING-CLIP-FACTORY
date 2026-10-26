import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager
from uuid import uuid4
import json

MIGRATION = Path(__file__).parent / "migrations" / "001_initial.sql"
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
        c.executescript(MIGRATION.read_text(encoding="utf-8"))
        c.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(1,?)",(now(),))
        c.execute("INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES('global_paused','false',?)",(now(),))
        c.execute("INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES('operating_mode','supervised',?)",(now(),))
def event(c, level, kind, message, details=None):
    c.execute("INSERT INTO system_events(id,level,event_type,message,details_json,created_at) VALUES(?,?,?,?,?,?)",
      (str(uuid4()),level,kind,message,json.dumps(details or {}),now()))
