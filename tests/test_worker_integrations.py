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

def _seed_youtube_processing_db(db_path: Path, scheduled_at=None):
    initialize(db_path)
    stamp=now()
    with sqlite3.connect(db_path) as db:
        db.execute("""INSERT INTO sources(id,source_url,platform,title,creator,game,discovered_at,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",("source-1","https://example.invalid/source","manual","Game source","Owner","Valorant",stamp,stamp))
        db.execute("""INSERT INTO source_permissions(source_id,status,license_type,permission_evidence,editing_allowed,commercial_use_allowed,reviewed_by_owner,updated_at)
            VALUES(?,?,?,?,?,?,?,?)""",("source-1","AUTHORIZED","permission","owner grant",1,1,1,stamp))
        db.execute("""INSERT INTO media_assets(id,source_id,original_path,file_name,file_size,sha256,state,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",("media-1","source-1","/tmp/authorized.mp4","authorized.mp4",10,"mediahash","PROBED",stamp))
        db.execute("""INSERT INTO published_videos
            (id,source_id,media_id,output_path,title,description,template_id,qc_status,publishing_status,youtube_video_id,created_at,updated_at,scheduled_publish_at,remote_processing_status)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("publication-1","source-1","media-1","/tmp/private.mp4","Game clip","", "portrait","PASS","PROCESSING","youtube-123",stamp,stamp,scheduled_at,"unknown"))
        db.execute("""INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            ("processing-check","CHECK_YOUTUBE_PROCESSING","RUNNING","source-1","media-1",
             json.dumps({"published_video_id":"publication-1","poll_attempt":0,"max_polls":20}),stamp,stamp))

class _FakeYouTubeResponse:
    def __init__(self, value): self.value=value
    def execute(self): return self.value

class _FakeYouTubeVideos:
    def __init__(self, response): self.response=response; self.update_calls=[]
    def list(self, **kwargs): return _FakeYouTubeResponse(self.response)
    def update(self, **kwargs):
        self.update_calls.append(kwargs)
        return _FakeYouTubeResponse({})

class _FakeYouTubeService:
    def __init__(self, response): self._videos=_FakeYouTubeVideos(response)
    def videos(self): return self._videos

def test_processing_poll_pending_queues_bounded_followup_without_public_release(tmp_path,monkeypatch):
    from datetime import datetime, timezone
    db_path=tmp_path/"processing-pending.sqlite3"
    _seed_youtube_processing_db(db_path)
    service=_FakeYouTubeService({"items":[{"id":"youtube-123","processingDetails":{"processingStatus":"processing"},"status":{"privacyStatus":"private"}}]})
    monkeypatch.setattr(worker_module,"youtube_service",lambda:service)
    job={"id":"processing-check","payload_json":json.dumps({"published_video_id":"publication-1","poll_attempt":0,"max_polls":20})}
    Worker(db_path,None,None,timeout=5).check_youtube_processing(job)
    with sqlite3.connect(db_path) as db:
        publication=db.execute("SELECT publishing_status,remote_processing_status FROM published_videos WHERE id='publication-1'").fetchone()
        old_job=db.execute("SELECT status FROM jobs WHERE id='processing-check'").fetchone()[0]
        followups=db.execute("SELECT id,status,next_attempt_at,payload_json FROM jobs WHERE job_type='CHECK_YOUTUBE_PROCESSING' AND id<>'processing-check'").fetchall()
        public_jobs=db.execute("SELECT COUNT(*) FROM jobs WHERE job_type='PUBLISH_YOUTUBE'").fetchone()[0]
    assert publication==("PROCESSING","processing")
    assert old_job=="COMPLETED"
    assert len(followups)==1
    assert followups[0][1]=="QUEUED" and followups[0][2]
    assert json.loads(followups[0][3])["poll_attempt"]==1
    assert public_jobs==0
    assert service.videos().update_calls==[]

def test_processing_success_schedules_fresh_public_job_after_old_deferral(tmp_path,monkeypatch):
    from datetime import datetime, timedelta, timezone
    db_path=tmp_path/"processing-succeeded.sqlite3"
    scheduled=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat(timespec="seconds")
    _seed_youtube_processing_db(db_path,scheduled_at=scheduled)
    service=_FakeYouTubeService({"items":[{"id":"youtube-123","processingDetails":{"processingStatus":"succeeded"},"status":{"privacyStatus":"private"}}]})
    monkeypatch.setattr(worker_module,"youtube_service",lambda:service)
    monkeypatch.setenv("GCF_ENABLE_YOUTUBE_UPLOADS","true")
    monkeypatch.setenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","true")
    stamp=now()
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO settings(key,value,updated_at) VALUES('youtube_uploads_enabled','true',?)",(stamp,))
        # Simulate a completed deferral from a previous release worker run.
        db.execute("""INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            ("old-deferred-public","PUBLISH_YOUTUBE","COMPLETED","source-1","media-1",
             json.dumps({"published_video_id":"publication-1","deferred":True}),stamp,stamp,stamp))
    job={"id":"processing-check","payload_json":json.dumps({"published_video_id":"publication-1","poll_attempt":1,"max_polls":20})}
    Worker(db_path,None,None,timeout=5).check_youtube_processing(job)
    with sqlite3.connect(db_path) as db:
        publication=db.execute("SELECT publishing_status,remote_processing_status FROM published_videos WHERE id='publication-1'").fetchone()
        jobs=db.execute("SELECT id,status,next_attempt_at,payload_json FROM jobs WHERE job_type='PUBLISH_YOUTUBE'").fetchall()
    assert publication==("SCHEDULED","succeeded")
    assert len(jobs)==2
    current=[x for x in jobs if x[0]!="old-deferred-public"][0]
    assert current[1]=="QUEUED"
    assert current[2] is not None
    assert json.loads(current[3])["published_video_id"]=="publication-1"
    assert service.videos().update_calls==[]
