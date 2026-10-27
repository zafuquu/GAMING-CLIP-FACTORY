from pathlib import Path
from datetime import datetime, timezone
import json
import sqlite3
import sys
import pytest
from fastapi.testclient import TestClient

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"))
from app.config import Settings
from app.main import create_app
from app.worker import Worker
import app.worker as worker_module

def _fixture(tmp_path):
    root=tmp_path/"project"; data=root/"data"; media=data/"media"
    cfg=Settings(root,data,media,data/"factory.sqlite3",None,None,5)
    client=TestClient(create_app(settings=cfg,start_worker=False))
    client.__enter__()
    source=client.post("/api/sources",json={"source_url":"https://example.com/processing-test","platform":"test","title":"Authorized test"})
    sid=source.json()["id"]
    rights=client.put(f"/api/sources/{sid}/rights",json={"status":"AUTHORIZED","license_type":"Synthetic test permission","permission_evidence":"Synthetic fixture only","editing_allowed":True,"commercial_use_allowed":True,"reviewed_by_owner":True})
    assert rights.status_code==200
    media.mkdir(parents=True,exist_ok=True)
    (media/"synthetic.mp4").write_bytes(b"synthetic registry fixture")
    imported=client.post("/api/media/import",json={"source_id":sid,"path":"synthetic.mp4"})
    assert imported.status_code==201,imported.text
    mid=imported.json()["id"]
    pub_id="pub-processing-test"
    stamp=datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(cfg.database_path) as db:
        db.execute("""INSERT INTO published_videos(id,source_id,media_id,output_path,title,description,template_id,qc_status,publishing_status,youtube_video_id,created_at,updated_at,remote_processing_status)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(pub_id,sid,mid,str(data/"outputs"/"test.mp4"),"Synthetic test","", "test","PASS","PROCESSING","youtube-id-123",stamp,stamp,"unknown"))
        db.execute("""INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)""",("processing-job","CHECK_YOUTUBE_PROCESSING","RUNNING",sid,mid,json.dumps({"published_video_id":pub_id,"poll_attempt":0,"max_polls":2}),stamp,stamp))
    return client,cfg,pub_id,sid,mid

class _Request:
    def __init__(self,payload): self.payload=payload
    def execute(self): return self.payload
class _Videos:
    def __init__(self,payload): self.payload=payload; self.calls=[]
    def list(self,**kwargs):
        self.calls.append(kwargs)
        assert kwargs["part"]=="processingDetails,status"
        return _Request(self.payload)
class _YouTubeService:
    def __init__(self,payload): self.payload=payload
    def videos(self): return _Videos(self.payload)

def test_youtube_processing_success_marks_private_video_ready(tmp_path,monkeypatch):
    client,cfg,pub_id,sid,mid=_fixture(tmp_path)
    payload={"items":[{"id":"youtube-id-123","processingDetails":{"processingStatus":"succeeded"},"status":{"privacyStatus":"private"}}]}
    monkeypatch.setattr(worker_module,"youtube_service",lambda:_YouTubeService(payload))
    worker=Worker(cfg.database_path,None,None,5)
    worker.check_youtube_processing({"id":"processing-job","payload_json":json.dumps({"published_video_id":pub_id,"poll_attempt":0,"max_polls":2})})
    with sqlite3.connect(cfg.database_path) as db:
        state=db.execute("SELECT publishing_status,remote_processing_status FROM published_videos WHERE id=?",(pub_id,)).fetchone()
        job=db.execute("SELECT status FROM jobs WHERE id='processing-job'").fetchone()
    assert tuple(state)==("READY_PRIVATE","succeeded")
    assert job[0]=="COMPLETED"
    client.__exit__(None,None,None)

def test_youtube_processing_failure_never_schedules_public_release(tmp_path,monkeypatch):
    client,cfg,pub_id,sid,mid=_fixture(tmp_path)
    payload={"items":[{"id":"youtube-id-123","processingDetails":{"processingStatus":"failed","processingFailureReason":"conversionFailed"},"status":{"privacyStatus":"private"}}]}
    monkeypatch.setattr(worker_module,"youtube_service",lambda:_YouTubeService(payload))
    worker=Worker(cfg.database_path,None,None,5)
    worker.check_youtube_processing({"id":"processing-job","payload_json":json.dumps({"published_video_id":pub_id,"poll_attempt":0,"max_polls":2})})
    with sqlite3.connect(cfg.database_path) as db:
        state=db.execute("SELECT publishing_status,remote_processing_status,processing_failure_reason FROM published_videos WHERE id=?",(pub_id,)).fetchone()
        jobs=db.execute("SELECT COUNT(*) FROM jobs WHERE job_type='PUBLISH_YOUTUBE'").fetchone()[0]
    assert state[0]=="PROCESSING_FAILED"
    assert state[1]=="failed"
    assert "conversionFailed" in state[2]
    assert jobs==0
    client.__exit__(None,None,None)

def test_youtube_processing_pending_queues_bounded_poll(tmp_path,monkeypatch):
    client,cfg,pub_id,sid,mid=_fixture(tmp_path)
    payload={"items":[{"id":"youtube-id-123","processingDetails":{"processingStatus":"processing"},"status":{"privacyStatus":"private"}}]}
    monkeypatch.setattr(worker_module,"youtube_service",lambda:_YouTubeService(payload))
    worker=Worker(cfg.database_path,None,None,5)
    worker.check_youtube_processing({"id":"processing-job","payload_json":json.dumps({"published_video_id":pub_id,"poll_attempt":0,"max_polls":2})})
    with sqlite3.connect(cfg.database_path) as db:
        state=db.execute("SELECT publishing_status,remote_processing_status FROM published_videos WHERE id=?",(pub_id,)).fetchone()
        polls=db.execute("SELECT COUNT(*) FROM jobs WHERE job_type='CHECK_YOUTUBE_PROCESSING' AND status='QUEUED'").fetchone()[0]
        job_status=db.execute("SELECT status FROM jobs WHERE id='processing-job'").fetchone()[0]
    assert tuple(state)==("PROCESSING","processing")
    assert polls==1
    assert job_status=="COMPLETED"
    client.__exit__(None,None,None)

def test_expired_rights_block_public_job_after_processing_succeeds(tmp_path,monkeypatch):
    monkeypatch.setenv("GCF_ENABLE_YOUTUBE_UPLOADS","true")
    monkeypatch.setenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","true")
    client,cfg,pub_id,sid,mid=_fixture(tmp_path)
    future=(datetime.now(timezone.utc)+__import__("datetime").timedelta(days=1)).isoformat()
    expired=(datetime.now(timezone.utc)-__import__("datetime").timedelta(days=1)).isoformat()
    with sqlite3.connect(cfg.database_path) as db:
        db.execute("UPDATE published_videos SET scheduled_publish_at=? WHERE id=?",(future,pub_id))
        db.execute("UPDATE source_permissions SET expires_at=? WHERE source_id=?",(expired,sid))
        db.execute("INSERT INTO settings(key,value,updated_at) VALUES('youtube_uploads_enabled','true',?)",(datetime.now(timezone.utc).isoformat(),))
    payload={"items":[{"id":"youtube-id-123","processingDetails":{"processingStatus":"succeeded"},"status":{"privacyStatus":"private"}}]}
    monkeypatch.setattr(worker_module,"youtube_service",lambda:_YouTubeService(payload))
    worker=Worker(cfg.database_path,None,None,5)
    worker.check_youtube_processing({"id":"processing-job","payload_json":json.dumps({"published_video_id":pub_id,"poll_attempt":0,"max_polls":2})})
    with sqlite3.connect(cfg.database_path) as db:
        state=db.execute("SELECT publishing_status,remote_processing_status FROM published_videos WHERE id=?",(pub_id,)).fetchone()
        public_jobs=db.execute("SELECT COUNT(*) FROM jobs WHERE job_type='PUBLISH_YOUTUBE' AND status='QUEUED'").fetchone()[0]
    assert tuple(state)==("BLOCKED_RIGHTS","succeeded")
    assert public_jobs==0
    client.__exit__(None,None,None)

def test_owner_can_recheck_timed_out_processing_without_creating_duplicate_jobs(tmp_path,monkeypatch):
    import app.main as main_module
    monkeypatch.setattr(main_module,"connected_status",lambda:{"connected":True,"channel_title":"Synthetic CI"})
    client,cfg,pub_id,sid,mid=_fixture(tmp_path)
    with sqlite3.connect(cfg.database_path) as db:
        db.execute("UPDATE published_videos SET publishing_status='PROCESSING_TIMEOUT',remote_processing_status='processing',processing_failure_reason='poll limit reached' WHERE id=?",(pub_id,))
        db.execute("UPDATE jobs SET status='COMPLETED',finished_at=? WHERE id='processing-job'",(datetime.now(timezone.utc).isoformat(),))
    response=client.post(f"/api/publishing/videos/{pub_id}/recheck-processing")
    assert response.status_code==202,response.text
    assert response.json()["status"]=="QUEUED"
    second=client.post(f"/api/publishing/videos/{pub_id}/recheck-processing")
    assert second.status_code==202,second.text
    assert second.json()["status"]=="ALREADY_QUEUED"
    with sqlite3.connect(cfg.database_path) as db:
        status=db.execute("SELECT publishing_status FROM published_videos WHERE id=?",(pub_id,)).fetchone()[0]
        jobs=db.execute("SELECT COUNT(*) FROM jobs WHERE job_type='CHECK_YOUTUBE_PROCESSING' AND status IN ('QUEUED','RUNNING')").fetchone()[0]
    assert status=="PROCESSING"
    assert jobs==1
    client.__exit__(None,None,None)

def test_future_rights_expiry_before_scheduled_time_blocks_public_release(tmp_path,monkeypatch):
    monkeypatch.setenv("GCF_ENABLE_YOUTUBE_UPLOADS","true")
    monkeypatch.setenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","true")
    client,cfg,pub_id,sid,mid=_fixture(tmp_path)
    now=datetime.now(timezone.utc)
    expiry=(now+__import__("datetime").timedelta(hours=1)).isoformat()
    scheduled=(now+__import__("datetime").timedelta(hours=3)).isoformat()
    with sqlite3.connect(cfg.database_path) as db:
        db.execute("UPDATE source_permissions SET expires_at=? WHERE source_id=?",(expiry,sid))
        db.execute("UPDATE published_videos SET scheduled_publish_at=? WHERE id=?",(scheduled,pub_id))
        db.execute("INSERT INTO settings(key,value,updated_at) VALUES('youtube_uploads_enabled','true',?)",(now.isoformat(),))
    payload={"items":[{"id":"youtube-id-123","processingDetails":{"processingStatus":"succeeded"},"status":{"privacyStatus":"private"}}]}
    monkeypatch.setattr(worker_module,"youtube_service",lambda:_YouTubeService(payload))
    worker=Worker(cfg.database_path,None,None,5)
    worker.check_youtube_processing({"id":"processing-job","payload_json":json.dumps({"published_video_id":pub_id,"poll_attempt":0,"max_polls":2})})
    with sqlite3.connect(cfg.database_path) as db:
        state=db.execute("SELECT publishing_status,remote_processing_status FROM published_videos WHERE id=?",(pub_id,)).fetchone()
        active=db.execute("SELECT COUNT(*) FROM jobs WHERE job_type='PUBLISH_YOUTUBE' AND status IN ('QUEUED','RUNNING')").fetchone()[0]
    assert tuple(state)==("BLOCKED_RIGHTS","succeeded")
    assert active==0
    client.__exit__(None,None,None)


def _attach_render_for_public_release(tmp_path,cfg,pub_id,sid,mid):
    import hashlib
    stamp=datetime.now(timezone.utc).isoformat(timespec="seconds")
    output_dir=cfg.data_dir/"outputs"
    output_dir.mkdir(parents=True,exist_ok=True)
    output=output_dir/"synthetic-public-release.mp4"
    output.write_bytes(b"synthetic rendered output; all YouTube calls are mocked")
    sha=hashlib.sha256(output.read_bytes()).hexdigest()
    candidate_id="candidate-public-release"
    render_id="render-public-release"
    with sqlite3.connect(cfg.database_path) as db:
        db.execute("""INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",(candidate_id,mid,0.0,5.0,"test","Public release fixture","RENDERED_QC_PASSED",stamp))
        db.execute("""INSERT INTO rendered_outputs(id,candidate_id,media_id,output_path,file_size,sha256,duration_seconds,width,height,
            video_codec,audio_codec,qc_status,qc_report_json,created_at,render_profile)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (render_id,candidate_id,mid,str(output),output.stat().st_size,sha,5.0,1080,1920,"h264","aac","PASS",'{"checks":{"nonempty":true}}',stamp,"fit_pad"))
        db.execute("""UPDATE published_videos SET candidate_id=?,rendered_output_id=?,output_path=?,qc_status='PASS',
            publishing_status='SCHEDULED',remote_processing_status='succeeded' WHERE id=?""",
            (candidate_id,render_id,str(output),pub_id))
        db.execute("UPDATE jobs SET status='COMPLETED',finished_at=? WHERE id='processing-job'",(stamp,))
    return output,sha,render_id

class _MutationVideos(_Videos):
    def __init__(self,payload):
        super().__init__(payload); self.updates=[]
    def update(self,**kwargs):
        self.updates.append(kwargs)
        return _Request({"id":kwargs.get("body",{}).get("id"),"status":{"privacyStatus":kwargs.get("body",{}).get("status",{}).get("privacyStatus")}})

class _MutationYouTubeService(_YouTubeService):
    def __init__(self,payload):
        super().__init__(payload); self.video_api=_MutationVideos(payload)
    def videos(self): return self.video_api

def _public_job(cfg,pub_id,scheduled):
    stamp=datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(cfg.database_path) as db:
        db.execute("""INSERT INTO jobs(id,job_type,status,payload_json,created_at,updated_at)
            VALUES('public-release-job','PUBLISH_YOUTUBE','RUNNING',?,?,?)""",
            (json.dumps({"published_video_id":pub_id,"publish_at":scheduled.isoformat()}),stamp,stamp))
    return {"id":"public-release-job","job_type":"PUBLISH_YOUTUBE","status":"RUNNING",
            "payload_json":json.dumps({"published_video_id":pub_id,"publish_at":scheduled.isoformat()})}

def _enable_public_release(tmp_path,monkeypatch,cfg):
    monkeypatch.setenv("GCF_ENABLE_YOUTUBE_UPLOADS","true")
    monkeypatch.setenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","true")
    stamp=datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(cfg.database_path) as db:
        db.execute("INSERT INTO settings(key,value,updated_at) VALUES('youtube_uploads_enabled','true',?) ON CONFLICT(key) DO UPDATE SET value='true',updated_at=excluded.updated_at",(stamp,))

def test_scheduled_public_worker_publishes_only_after_all_live_gates_pass(tmp_path,monkeypatch):
    client,cfg,pub_id,sid,mid=_fixture(tmp_path)
    output,sha,render_id=_attach_render_for_public_release(tmp_path,cfg,pub_id,sid,mid)
    _enable_public_release(tmp_path,monkeypatch,cfg)
    scheduled=datetime.now(timezone.utc)-__import__("datetime").timedelta(minutes=1)
    with sqlite3.connect(cfg.database_path) as db:
        db.execute("UPDATE published_videos SET scheduled_publish_at=? WHERE id=?",(scheduled.isoformat(timespec="seconds"),pub_id))
    payload={"items":[{"id":"youtube-id-123","processingDetails":{"processingStatus":"succeeded"},"status":{"privacyStatus":"private"}}]}
    fake=_MutationYouTubeService(payload)
    monkeypatch.setattr(worker_module,"youtube_service",lambda:fake)
    _public_job(cfg,pub_id,scheduled)
    worker=Worker(cfg.database_path,None,None,5)
    worker.publish_youtube({"id":"public-release-job","payload_json":json.dumps({"published_video_id":pub_id,"publish_at":scheduled.isoformat()})})
    with sqlite3.connect(cfg.database_path) as db:
        state=db.execute("SELECT publishing_status,remote_processing_status FROM published_videos WHERE id=?",(pub_id,)).fetchone()
        job=db.execute("SELECT status FROM jobs WHERE id='public-release-job'").fetchone()
    assert tuple(state)==("PUBLISHED","succeeded")
    assert job[0]=="COMPLETED"
    assert len(fake.video_api.updates)==1
    assert fake.video_api.updates[0]["body"]["status"]["privacyStatus"]=="public"
    client.__exit__(None,None,None)

def test_scheduled_public_worker_does_not_mutate_remote_before_scheduled_time(tmp_path,monkeypatch):
    client,cfg,pub_id,sid,mid=_fixture(tmp_path)
    _attach_render_for_public_release(tmp_path,cfg,pub_id,sid,mid)
    _enable_public_release(tmp_path,monkeypatch,cfg)
    scheduled=datetime.now(timezone.utc)+__import__("datetime").timedelta(hours=2)
    with sqlite3.connect(cfg.database_path) as db:
        db.execute("UPDATE published_videos SET scheduled_publish_at=? WHERE id=?",(scheduled.isoformat(timespec="seconds"),pub_id))
    fake=_MutationYouTubeService({"items":[{"id":"youtube-id-123","processingDetails":{"processingStatus":"succeeded"},"status":{"privacyStatus":"private"}}]})
    monkeypatch.setattr(worker_module,"youtube_service",lambda:fake)
    _public_job(cfg,pub_id,scheduled)
    Worker(cfg.database_path,None,None,5).publish_youtube({"id":"public-release-job","payload_json":json.dumps({"published_video_id":pub_id,"publish_at":scheduled.isoformat()})})
    with sqlite3.connect(cfg.database_path) as db:
        state=db.execute("SELECT publishing_status FROM published_videos WHERE id=?",(pub_id,)).fetchone()[0]
        job=db.execute("SELECT status,next_attempt_at FROM jobs WHERE id='public-release-job'").fetchone()
    assert state=="SCHEDULED"
    assert job[0]=="QUEUED"
    assert datetime.fromisoformat(job[1])>=scheduled
    assert fake.video_api.updates==[]
    assert fake.video_api.calls==[]
    client.__exit__(None,None,None)

def test_scheduled_public_worker_defers_if_live_processing_is_pending(tmp_path,monkeypatch):
    client,cfg,pub_id,sid,mid=_fixture(tmp_path)
    _attach_render_for_public_release(tmp_path,cfg,pub_id,sid,mid)
    _enable_public_release(tmp_path,monkeypatch,cfg)
    scheduled=datetime.now(timezone.utc)-__import__("datetime").timedelta(minutes=1)
    with sqlite3.connect(cfg.database_path) as db:
        db.execute("UPDATE published_videos SET scheduled_publish_at=? WHERE id=?",(scheduled.isoformat(timespec="seconds"),pub_id))
    fake=_MutationYouTubeService({"items":[{"id":"youtube-id-123","processingDetails":{"processingStatus":"processing"},"status":{"privacyStatus":"private"}}]})
    monkeypatch.setattr(worker_module,"youtube_service",lambda:fake)
    _public_job(cfg,pub_id,scheduled)
    Worker(cfg.database_path,None,None,5).publish_youtube({"id":"public-release-job","payload_json":json.dumps({"published_video_id":pub_id,"publish_at":scheduled.isoformat()})})
    with sqlite3.connect(cfg.database_path) as db:
        state=db.execute("SELECT publishing_status,remote_processing_status FROM published_videos WHERE id=?",(pub_id,)).fetchone()
        release_job=db.execute("SELECT status FROM jobs WHERE id='public-release-job'").fetchone()[0]
        polls=db.execute("SELECT COUNT(*) FROM jobs WHERE job_type='CHECK_YOUTUBE_PROCESSING' AND status='QUEUED'").fetchone()[0]
    assert tuple(state)==("PROCESSING","processing")
    assert release_job=="COMPLETED"
    assert polls==1
    assert fake.video_api.updates==[]
    client.__exit__(None,None,None)

def test_scheduled_public_worker_rechecks_current_rights_before_remote_update(tmp_path,monkeypatch):
    client,cfg,pub_id,sid,mid=_fixture(tmp_path)
    _attach_render_for_public_release(tmp_path,cfg,pub_id,sid,mid)
    _enable_public_release(tmp_path,monkeypatch,cfg)
    scheduled=datetime.now(timezone.utc)-__import__("datetime").timedelta(minutes=1)
    with sqlite3.connect(cfg.database_path) as db:
        db.execute("UPDATE published_videos SET scheduled_publish_at=? WHERE id=?",(scheduled.isoformat(timespec="seconds"),pub_id))
        db.execute("UPDATE source_permissions SET status='REVOKED' WHERE source_id=?",(sid,))
    fake=_MutationYouTubeService({"items":[{"id":"youtube-id-123","processingDetails":{"processingStatus":"succeeded"},"status":{"privacyStatus":"private"}}]})
    monkeypatch.setattr(worker_module,"youtube_service",lambda:fake)
    _public_job(cfg,pub_id,scheduled)
    Worker(cfg.database_path,None,None,5).publish_youtube({"id":"public-release-job","payload_json":json.dumps({"published_video_id":pub_id,"publish_at":scheduled.isoformat()})})
    with sqlite3.connect(cfg.database_path) as db:
        state=db.execute("SELECT publishing_status FROM published_videos WHERE id=?",(pub_id,)).fetchone()[0]
    assert state=="BLOCKED_RIGHTS"
    assert fake.video_api.updates==[]
    assert fake.video_api.calls==[]
    client.__exit__(None,None,None)

def test_scheduled_public_worker_refuses_modified_render_hash(tmp_path,monkeypatch):
    client,cfg,pub_id,sid,mid=_fixture(tmp_path)
    output,sha,render_id=_attach_render_for_public_release(tmp_path,cfg,pub_id,sid,mid)
    _enable_public_release(tmp_path,monkeypatch,cfg)
    scheduled=datetime.now(timezone.utc)-__import__("datetime").timedelta(minutes=1)
    with sqlite3.connect(cfg.database_path) as db:
        db.execute("UPDATE published_videos SET scheduled_publish_at=? WHERE id=?",(scheduled.isoformat(timespec="seconds"),pub_id))
    output.write_bytes(b"tampered after QC")
    fake=_MutationYouTubeService({"items":[{"id":"youtube-id-123","processingDetails":{"processingStatus":"succeeded"},"status":{"privacyStatus":"private"}}]})
    monkeypatch.setattr(worker_module,"youtube_service",lambda:fake)
    _public_job(cfg,pub_id,scheduled)
    with pytest.raises(PermissionError,match="hash"):
        Worker(cfg.database_path,None,None,5).publish_youtube({"id":"public-release-job","payload_json":json.dumps({"published_video_id":pub_id,"publish_at":scheduled.isoformat()})})
    assert fake.video_api.updates==[]
    assert fake.video_api.calls==[]
    client.__exit__(None,None,None)

def test_scheduled_public_worker_respects_global_pause(tmp_path,monkeypatch):
    client,cfg,pub_id,sid,mid=_fixture(tmp_path)
    _attach_render_for_public_release(tmp_path,cfg,pub_id,sid,mid)
    _enable_public_release(tmp_path,monkeypatch,cfg)
    scheduled=datetime.now(timezone.utc)-__import__("datetime").timedelta(minutes=1)
    with sqlite3.connect(cfg.database_path) as db:
        db.execute("UPDATE published_videos SET scheduled_publish_at=? WHERE id=?",(scheduled.isoformat(timespec="seconds"),pub_id))
        db.execute("INSERT INTO settings(key,value,updated_at) VALUES('global_paused','true',?) ON CONFLICT(key) DO UPDATE SET value='true',updated_at=excluded.updated_at",(datetime.now(timezone.utc).isoformat(),))
    fake=_MutationYouTubeService({"items":[{"id":"youtube-id-123","processingDetails":{"processingStatus":"succeeded"},"status":{"privacyStatus":"private"}}]})
    monkeypatch.setattr(worker_module,"youtube_service",lambda:fake)
    _public_job(cfg,pub_id,scheduled)
    Worker(cfg.database_path,None,None,5).publish_youtube({"id":"public-release-job","payload_json":json.dumps({"published_video_id":pub_id,"publish_at":scheduled.isoformat()})})
    with sqlite3.connect(cfg.database_path) as db:
        job=db.execute("SELECT status,next_attempt_at FROM jobs WHERE id='public-release-job'").fetchone()
        state=db.execute("SELECT publishing_status FROM published_videos WHERE id=?",(pub_id,)).fetchone()[0]
    assert job[0]=="QUEUED"
    assert state=="SCHEDULED"
    assert fake.video_api.updates==[]
    assert fake.video_api.calls==[]
    client.__exit__(None,None,None)
