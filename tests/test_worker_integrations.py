import json
import sqlite3
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"))
from app import worker as worker_module
from app.db import initialize, now
from app.worker import Worker

def _seed_analytics_db(db_path: Path):
    initialize(db_path)
    stamp=now()
    with sqlite3.connect(db_path) as db:
        db.execute("""INSERT INTO published_videos
            (id,output_path,title,template_id,qc_status,publishing_status,youtube_video_id,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            ("published-1","/tmp/private.mp4","Private test video","test","PASS","READY_PRIVATE","yt-video-1",stamp,stamp))
        db.execute("""INSERT INTO jobs(id,job_type,status,payload_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?)""",
            ("analytics-job","SYNC_YOUTUBE_ANALYTICS","QUEUED",
             json.dumps({"start_date":"2026-10-01","end_date":"2026-10-08"}),stamp,stamp))

def test_analytics_worker_dispatches_and_persists_returned_values(tmp_path,monkeypatch):
    db_path=tmp_path/"analytics.sqlite3"
    _seed_analytics_db(db_path)
    remote_rows=[
        {"video":"yt-video-1","views":17,"likes":3,"comments":1,"averageViewPercentage":68.5},
        {"video":"not-in-local-history","views":999},
        {"views":123},
    ]
    captured={}
    def fake_fetch(start_date,end_date,max_results=200):
        captured.update(start_date=start_date,end_date=end_date,max_results=max_results)
        return remote_rows
    monkeypatch.setattr(worker_module,"fetch_video_analytics",fake_fetch)
    worker=Worker(db_path,None,None,timeout=5)
    assert worker.one() is True
    assert captured=={"start_date":"2026-10-01","end_date":"2026-10-08","max_results":200}
    with sqlite3.connect(db_path) as db:
        status,payload=db.execute("SELECT status,payload_json FROM jobs WHERE id='analytics-job'").fetchone()
        snapshots=db.execute("SELECT published_video_id,provider,metrics_json FROM analytics_snapshots").fetchall()
    assert status=="COMPLETED"
    summary=json.loads(payload)
    assert summary["rows_returned"]==3
    assert summary["snapshots_saved"]==1
    assert summary["unmatched_video_rows"]==1
    assert len(snapshots)==1
    assert snapshots[0][0]=="published-1"
    assert snapshots[0][1]=="youtube_analytics_api"
    persisted=json.loads(snapshots[0][2])
    assert persisted["start_date"]=="2026-10-01"
    assert persisted["end_date"]=="2026-10-08"
    assert persisted["metrics"]==remote_rows[0]
