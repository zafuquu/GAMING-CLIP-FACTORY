from pathlib import Path
from datetime import datetime, timedelta, timezone
import sqlite3
import json
import pytest
import sys
from fastapi.testclient import TestClient
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"))
from app.config import Settings
from app.main import create_app

def setup_client(tmp_path):
    root=tmp_path/"project"; data=root/"data"; media=data/"media"
    cfg=Settings(root,data,media,data/"test.sqlite3",None,None,5)
    client=TestClient(create_app(settings=cfg,start_worker=False))
    client.__enter__()
    return client,media

def add_source(c):
    r=c.post("/api/sources",json={"source_url":"https://example.com/clip/123","platform":"manual","title":"Test"})
    assert r.status_code==201,r.text
    return r.json()["id"]
def authorize(c,sid):
    return c.put(f"/api/sources/{sid}/rights",json={"status":"AUTHORIZED","license_type":"Written permission","permission_evidence":"https://example.com/proof","editing_allowed":True,"commercial_use_allowed":True,"reviewed_by_owner":True})

def test_health_and_empty_dashboard(tmp_path):
    c,_=setup_client(tmp_path)
    assert c.get("/api/health").json()["status"]=="ok"
    d=c.get("/api/dashboard").json()
    assert d["counts"]["sources_total"]==0 and d["counts"]["published_videos"]==0
    c.__exit__(None,None,None)
def test_built_dashboard_is_served_by_local_api(tmp_path):
    root=tmp_path/"project"
    dist=root/"frontend"/"dist"
    dist.mkdir(parents=True)
    (dist/"index.html").write_text("<!doctype html><html><body>GCF production dashboard</body></html>",encoding="utf-8")
    data=root/"data"
    cfg=Settings(root,data,data/"media",data/"test.sqlite3",None,None,5)
    with TestClient(create_app(settings=cfg,start_worker=False)) as c:
        home=c.get("/")
        assert home.status_code==200,home.text
        assert "GCF production dashboard" in home.text
        health=c.get("/api/health")
        assert health.status_code==200,health.text
        assert health.json()["status"]=="ok"

def test_worker_survives_when_failure_event_cannot_be_written(tmp_path,monkeypatch):
    from app.worker import Worker
    import app.worker as worker_module

    worker=Worker(tmp_path/"missing"/"worker.sqlite3",None,None,5)
    attempts=[]
    def failing_work():
        attempts.append("attempted")
        raise RuntimeError("simulated worker failure")
    worker.one=failing_work

    class StopAfterWait:
        stopped=False
        def is_set(self):
            return self.stopped
        def wait(self,timeout=None):
            self.stopped=True
            return True
        def set(self):
            self.stopped=True

    worker.stop_event=StopAfterWait()
    def unavailable_database(_path):
        raise sqlite3.OperationalError("simulated database outage")
    monkeypatch.setattr(worker_module,"connect",unavailable_database)
    # Must swallow the secondary logging failure, wait, and stop cleanly instead of
    # letting an exception escape the worker thread and permanently killing automation.
    worker.run()
    assert attempts==["attempted"]
    assert worker.stop_event.is_set()

def test_readiness_reports_missing_media_toolchain(tmp_path):
    c,_=setup_client(tmp_path)
    response=c.get("/api/readiness")
    assert response.status_code==200,response.text
    payload=response.json()
    assert payload["ready"] is False
    assert payload["status"]=="degraded"
    assert payload["checks"]["database"]["ready"] is True
    assert payload["checks"]["worker"]["enabled"] is False
    assert payload["checks"]["worker"]["required"] is False
    assert payload["checks"]["ffmpeg"]["ready"] is False
    assert payload["checks"]["ffprobe"]["ready"] is False
    assert payload["checks"]["database_directory"]["ready"] is True
    assert payload["checks"]["media_root"]["ready"] is True
    assert payload["checks"]["output_directory"]["ready"] is True
    c.__exit__(None,None,None)

def test_readiness_reports_healthy_media_toolchain(tmp_path,monkeypatch):
    from types import SimpleNamespace
    root=tmp_path/"ready-project"; data=root/"data"; media=data/"media"
    cfg=Settings(root,data,media,data/"test.sqlite3","ffmpeg-test","ffprobe-test",5)
    def fake_run(args,**kwargs):
        assert args[1]=="-version"
        assert kwargs.get("shell") is False
        return SimpleNamespace(returncode=0,stdout=f"{args[0]} version 7.1.2",stderr="")
    monkeypatch.setattr("app.main.subprocess.run",fake_run)
    with TestClient(create_app(settings=cfg,start_worker=False)) as c:
        response=c.get("/api/readiness")
        assert response.status_code==200,response.text
        payload=response.json()
        assert payload["ready"] is True
        assert payload["status"]=="ready"
        assert payload["checks"]["worker"]["enabled"] is False
        assert payload["checks"]["worker"]["ready"] is True
        assert payload["checks"]["ffmpeg"]["version"]=="ffmpeg-test version 7.1.2"
        assert payload["checks"]["ffprobe"]["version"]=="ffprobe-test version 7.1.2"
        assert all(payload["checks"][k]["ready"] for k in ("database","ffmpeg","ffprobe","database_directory","media_root","output_directory"))

def test_new_source_starts_unknown_and_duplicate_blocked(tmp_path):
    c,_=setup_client(tmp_path); sid=add_source(c)
    assert c.get("/api/sources").json()[0]["rights_status"]=="UNKNOWN"
    assert c.post("/api/sources",json={"source_url":"https://example.com/clip/123","platform":"manual"}).status_code==409
    c.__exit__(None,None,None)
def test_authorization_requires_explicit_evidence(tmp_path):
    c,_=setup_client(tmp_path); sid=add_source(c)
    assert c.put(f"/api/sources/{sid}/rights",json={"status":"AUTHORIZED"}).status_code==422
    c.__exit__(None,None,None)
def test_media_import_blocked_without_authorization(tmp_path):
    c,media=setup_client(tmp_path); sid=add_source(c); media.mkdir(parents=True,exist_ok=True); (media/"clip.mp4").write_bytes(b"test")
    r=c.post("/api/media/import",json={"source_id":sid,"path":"clip.mp4"})
    assert r.status_code==409
    c.__exit__(None,None,None)
def test_media_import_path_escape_blocked(tmp_path):
    c,media=setup_client(tmp_path); sid=add_source(c); authorize(c,sid); media.mkdir(parents=True,exist_ok=True)
    outside=media.parent/"escape.mp4"; outside.write_bytes(b"test")
    assert c.post("/api/media/import",json={"source_id":sid,"path":str(outside)}).status_code==400
    c.__exit__(None,None,None)
def test_pause_blocks_analysis_queue(tmp_path):
    c,media=setup_client(tmp_path); sid=add_source(c); authorize(c,sid); media.mkdir(parents=True,exist_ok=True); (media/"clip.mp4").write_bytes(b"test")
    imported=c.post("/api/media/import",json={"source_id":sid,"path":"clip.mp4"}); assert imported.status_code==201
    c.post("/api/system/pause",json={"paused":True})
    assert c.post("/api/media/"+imported.json()["id"]+"/analyze").status_code==423
    c.__exit__(None,None,None)
def test_autonomous_mode_is_refused(tmp_path):
    c,_=setup_client(tmp_path)
    assert c.put("/api/settings/mode",json={"mode":"autonomous"}).status_code==422
    c.__exit__(None,None,None)

def test_expired_permission_blocks_media_import(tmp_path):
    c,media=setup_client(tmp_path); sid=add_source(c)
    media.mkdir(parents=True,exist_ok=True); (media/"clip.mp4").write_bytes(b"test")
    expired=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
    db=c.app.state.settings.database_path
    with sqlite3.connect(db) as raw:
        raw.execute("UPDATE source_permissions SET status='AUTHORIZED',license_type='Written permission',permission_evidence='https://example.com/proof',editing_allowed=1,commercial_use_allowed=1,reviewed_by_owner=1,expires_at=? WHERE source_id=?",(expired,sid))
    listed=c.get("/api/sources").json()
    assert next(x for x in listed if x["id"]==sid)["rights_status"]=="EXPIRED"
    response=c.post("/api/media/import",json={"source_id":sid,"path":"clip.mp4"})
    assert response.status_code==409
    c.__exit__(None,None,None)

def test_render_migration_is_applied(tmp_path):
    c,_=setup_client(tmp_path)
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        tables={row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        versions={row[0] for row in raw.execute("SELECT version FROM schema_migrations")}
    assert "rendered_outputs" in tables
    assert "transcripts" in tables and "content_plans" in tables and "caption_segments" in tables
    assert "media_folder_watches" in tables and "watched_media_files" in tables
    watch_columns={row[1] for row in raw.execute("PRAGMA table_info(media_folder_watches)")}
    assert "recursive" in watch_columns
    columns={row[1] for row in raw.execute("PRAGMA table_info(rendered_outputs)")}
    assert {"render_profile","thumbnail_path","thumbnail_sha256","thumbnail_width","thumbnail_height"}.issubset(columns)
    assert {1,2,3,4,5,6,7,8,9,10,11}.issubset(versions)
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        render_columns={row[1] for row in raw.execute("PRAGMA table_info(rendered_outputs)")}
    assert {"render_profile","thumbnail_path","thumbnail_sha256","thumbnail_width","thumbnail_height"}.issubset(render_columns)
    c.__exit__(None,None,None)

def test_candidate_rejection_is_persisted(tmp_path):
    c,media=setup_client(tmp_path); sid=add_source(c); authorize(c,sid); media.mkdir(parents=True,exist_ok=True)
    (media/"clip.mp4").write_bytes(b"synthetic placeholder bytes for registry-only test")
    imported=c.post("/api/media/import",json={"source_id":sid,"path":"clip.mp4"})
    assert imported.status_code==201,imported.text
    mid=imported.json()["id"]
    cid="candidate-test-id"
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        raw.execute("INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (cid,mid,0.0,5.0,"test","Test candidate only","NEEDS_REVIEW","2026-01-01T00:00:00+00:00"))
    response=c.post(f"/api/candidates/{cid}/decision",json={"decision":"reject","notes":"Not a usable highlight"})
    assert response.status_code==202,response.text
    assert response.json()["state"]=="REJECTED"
    assert next(x for x in c.get("/api/candidates").json() if x["id"]==cid)["state"]=="REJECTED"
    c.__exit__(None,None,None)

def test_twitch_discovery_requires_credentials(tmp_path,monkeypatch):
    monkeypatch.delenv("GCF_TWITCH_CLIENT_ID",raising=False)
    monkeypatch.delenv("GCF_TWITCH_CLIENT_SECRET",raising=False)
    c,_=setup_client(tmp_path)
    r=c.post("/api/discovery/twitch",json={"game_queries":["Valorant"]})
    assert r.status_code==503
    c.__exit__(None,None,None)

def test_twitch_schedule_requires_credentials(tmp_path,monkeypatch):
    monkeypatch.delenv("GCF_TWITCH_CLIENT_ID",raising=False)
    monkeypatch.delenv("GCF_TWITCH_CLIENT_SECRET",raising=False)
    c,_=setup_client(tmp_path)
    r=c.put("/api/discovery/twitch/schedule",json={"enabled":True,"game_queries":["Valorant"],"interval_minutes":60})
    assert r.status_code==503
    c.__exit__(None,None,None)

def test_uploads_are_disabled_by_default(tmp_path,monkeypatch):
    monkeypatch.delenv("GCF_ENABLE_YOUTUBE_UPLOADS",raising=False)
    c,_=setup_client(tmp_path)
    status=c.get("/api/publishing/status").json()
    assert status["uploads_enabled"] is False
    assert status["upload_environment_gate"] is False
    r=c.post("/api/publishing/schedule",json={"rendered_output_id":"not-a-real-output","title":"Test private upload"})
    assert r.status_code==403
    c.__exit__(None,None,None)

def test_interrupted_upload_requires_reconciliation(tmp_path):
    from app.db import recover_interrupted_jobs
    c,_=setup_client(tmp_path)
    db=c.app.state.settings.database_path
    with sqlite3.connect(db) as raw:
        raw.execute("INSERT INTO jobs(id,job_type,status,payload_json,created_at,updated_at) VALUES('uncertain-upload','UPLOAD_YOUTUBE','RUNNING','{}','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')")
    recover_interrupted_jobs(db)
    with sqlite3.connect(db) as raw:
        status=raw.execute("SELECT status FROM jobs WHERE id='uncertain-upload'").fetchone()[0]
    assert status=="RECONCILE_REQUIRED"
    c.__exit__(None,None,None)

def test_transcription_is_opt_in(tmp_path,monkeypatch):
    monkeypatch.delenv("GCF_ENABLE_TRANSCRIPTION",raising=False)
    c,_=setup_client(tmp_path)
    response=c.post("/api/media/fake-media-id/transcribe")
    assert response.status_code==403
    c.__exit__(None,None,None)

def test_integrations_status_does_not_expose_credentials(tmp_path,monkeypatch):
    monkeypatch.setenv("GCF_TWITCH_CLIENT_ID","example-id")
    monkeypatch.setenv("GCF_TWITCH_CLIENT_SECRET","example-secret")
    c,_=setup_client(tmp_path)
    response=c.get("/api/integrations/status")
    assert response.status_code==200
    body=response.json()
    serialized=str(body)
    assert "example-secret" not in serialized
    assert "example-id" not in serialized
    assert body["twitch"]["configured"] is True
    assert "transcription" in body
    c.__exit__(None,None,None)

def test_youtube_metadata_discovery_requires_configuration(tmp_path,monkeypatch):
    import app.main as main_module
    monkeypatch.setattr(main_module,"youtube_discovery_available",lambda:False)
    c,_=setup_client(tmp_path)
    assert c.post("/api/discovery/youtube",json={"game_queries":["Valorant"]}).status_code==503
    assert c.put("/api/discovery/youtube/schedule",json={"enabled":True,"game_queries":["Valorant"],"interval_minutes":60}).status_code==503
    c.__exit__(None,None,None)

def test_youtube_metadata_discovery_queues_without_claiming_reuse_rights(tmp_path,monkeypatch):
    import app.main as main_module
    monkeypatch.setattr(main_module,"youtube_discovery_available",lambda:True)
    c,_=setup_client(tmp_path)
    response=c.post("/api/discovery/youtube",json={"game_queries":["Valorant","Apex Legends"],"days":7,"max_results":50})
    assert response.status_code==202,response.text
    payload=response.json()
    assert payload["status"]=="QUEUED"
    assert payload["rights_status"]=="UNKNOWN"
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        job=raw.execute("SELECT job_type,status FROM jobs WHERE id=?",(payload["id"],)).fetchone()
    assert tuple(job)==("DISCOVER_YOUTUBE_VIDEOS","QUEUED")
    c.__exit__(None,None,None)

def test_render_review_status_can_be_previewed_but_is_not_publishable(tmp_path):
    c,media=setup_client(tmp_path); sid=add_source(c); authorize(c,sid); media.mkdir(parents=True,exist_ok=True)
    (media/"clip.mp4").write_bytes(b"authorized test media registry bytes")
    imported=c.post("/api/media/import",json={"source_id":sid,"path":"clip.mp4"})
    assert imported.status_code==201,imported.text
    mid=imported.json()["id"]
    cid="candidate-review-preview"
    output_id="render-review-preview"
    db=c.app.state.settings.database_path
    outdir=db.parent/"outputs"; outdir.mkdir(parents=True,exist_ok=True)
    outfile=outdir/"review-preview.mp4"; outfile.write_bytes(b"preview fixture bytes")
    with sqlite3.connect(db) as raw:
        raw.execute("INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (cid,mid,0.0,5.0,"test","QC review candidate","RENDERED_QC_REVIEW","2026-01-01T00:00:00+00:00"))
        raw.execute("""INSERT INTO rendered_outputs(id,candidate_id,media_id,output_path,file_size,sha256,duration_seconds,width,height,video_codec,audio_codec,qc_status,qc_report_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (output_id,cid,mid,str(outfile),outfile.stat().st_size,"hash-fixture",5.0,1080,1920,"h264","aac","REVIEW",'{"warnings":["black_interval_detected"]}',"2026-01-01T00:00:00+00:00"))
    preview=c.get(f"/api/renders/{output_id}/file")
    assert preview.status_code==200
    assert preview.content==b"preview fixture bytes"
    c.__exit__(None,None,None)

def test_vision_disabled_by_default(tmp_path,monkeypatch):
    monkeypatch.delenv("GCF_ENABLE_VISION_ANALYSIS",raising=False)
    c,_=setup_client(tmp_path)
    status=c.get("/api/vision/status").json()
    assert status["enabled"] is False
    assert status["automatic_approval"] is False
    assert c.post("/api/candidates/unknown/analyze-vision").status_code==403
    c.__exit__(None,None,None)

def test_vision_queue_requires_rights_and_local_ffmpeg(tmp_path,monkeypatch):
    import app.main as main_module
    monkeypatch.setattr(main_module,"vision_status",lambda:{"ready":True,"model":"test-vision","enabled":True,"automatic_approval":False})
    root=tmp_path/"project"; data=root/"data"; media=data/"media"
    cfg=Settings(root,data,media,data/"vision.sqlite3","/fake/ffmpeg",None,5)
    from fastapi.testclient import TestClient
    c=TestClient(create_app(settings=cfg,start_worker=False)); c.__enter__()
    sid=add_source(c)
    media.mkdir(parents=True,exist_ok=True); (media/"clip.mp4").write_bytes(b"synthetic bytes")
    imported=c.post("/api/media/import",json={"source_id":sid,"path":"clip.mp4"})
    assert imported.status_code==409
    authorize(c,sid)
    imported=c.post("/api/media/import",json={"source_id":sid,"path":"clip.mp4"})
    assert imported.status_code==201
    mid=imported.json()["id"]; cid="vision-queue-candidate"; stamp="2026-10-26T00:00:00+00:00"
    with sqlite3.connect(cfg.database_path) as db:
        db.execute("INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at) VALUES(?,?,?,?,?,?,?,?)",(cid,mid,0.0,4.0,"test","fixture","NEEDS_REVIEW",stamp))
    result=c.post(f"/api/candidates/{cid}/analyze-vision")
    assert result.status_code==202,result.text
    assert result.json()["automatic_approval"] is False
    with sqlite3.connect(cfg.database_path) as db:
        job=db.execute("SELECT job_type,status FROM jobs WHERE id=?",(result.json()["id"],)).fetchone()
    assert tuple(job)==("ANALYZE_CANDIDATE_VISION","QUEUED")
    c.__exit__(None,None,None)

def _add_scheduled_public_fixture(c,media,db_name="scheduled.mp4"):
    sid=add_source(c); assert authorize(c,sid).status_code==200
    media.mkdir(parents=True,exist_ok=True)
    source_file=media/"authorized.mp4"; source_file.write_bytes(b"authorized fake media")
    imported=c.post("/api/media/import",json={"source_id":sid,"path":"authorized.mp4"})
    assert imported.status_code==201,imported.text
    mid=imported.json()["id"]
    cfg=c.app.state.settings; data_dir=cfg.database_path.parent
    output_dir=data_dir/"outputs"; output_dir.mkdir(parents=True,exist_ok=True)
    output=output_dir/db_name; output.write_bytes(b"synthetic qc fixture")
    import hashlib
    sha=hashlib.sha256(output.read_bytes()).hexdigest()
    cid="schedule-candidate-"+db_name.replace(".mp4","")
    rid="schedule-render-"+db_name.replace(".mp4","")
    pubid="schedule-pub-"+db_name.replace(".mp4","")
    stamp=datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(cfg.database_path) as raw:
        raw.execute("INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at) VALUES(?,?,?,?,?,?,?,?)",(cid,mid,0.0,5.0,"test","scheduled fixture","RENDERED_QC_PASSED",stamp))
        raw.execute("""INSERT INTO rendered_outputs(id,candidate_id,media_id,output_path,file_size,sha256,duration_seconds,width,height,video_codec,audio_codec,qc_status,qc_report_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(rid,cid,mid,str(output),output.stat().st_size,sha,5.0,1080,1920,"h264","aac","PASS",'{"passed":true}',stamp))
        future=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
        raw.execute("""INSERT INTO published_videos(id,source_id,media_id,candidate_id,output_path,title,description,template_id,qc_status,publishing_status,youtube_video_id,created_at,updated_at,rendered_output_id,scheduled_publish_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?)""",(pubid,sid,mid,cid,str(output),"Test scheduled publication","", "test","PASS","UPLOAD_PENDING",stamp,stamp,rid,future))
    return pubid,rid,future

def test_cancel_scheduled_public_release_clears_queued_upload_intent(tmp_path):
    c,media=setup_client(tmp_path)
    pubid,_,future=_add_scheduled_public_fixture(c,media)
    jobid="queued-scheduled-upload"
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        raw.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at) SELECT ?, 'UPLOAD_YOUTUBE','QUEUED',source_id,media_id,?,created_at,updated_at FROM published_videos WHERE id=?",
                    (jobid,json.dumps({"published_video_id":pubid,"publish_at":future,"publication_mode":"scheduled_public","tags":[]}),pubid))
        raw.execute("INSERT INTO jobs(id,job_type,status,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    ("queued-public-release","PUBLISH_YOUTUBE","QUEUED",json.dumps({"published_video_id":pubid,"publish_at":future}),datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc).isoformat()))
    response=c.post(f"/api/publishing/videos/{pubid}/cancel-public-release")
    assert response.status_code==200,response.text
    assert response.json()["public_release_cancelled"] is True
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        schedule=raw.execute("SELECT scheduled_publish_at,publishing_status FROM published_videos WHERE id=?",(pubid,)).fetchone()
        upload=json.loads(raw.execute("SELECT payload_json FROM jobs WHERE id=?",(jobid,)).fetchone()[0])
        pub_job=raw.execute("SELECT status FROM jobs WHERE id='queued-public-release'").fetchone()[0]
    assert schedule==(None,"UPLOAD_PENDING")
    assert upload["publish_at"] is None
    assert upload["publication_mode"]=="private"
    assert pub_job=="CANCELLED"
    c.__exit__(None,None,None)

def test_reschedule_public_release_updates_upload_payload_and_checks_rights(tmp_path,monkeypatch):
    monkeypatch.setenv("GCF_ENABLE_YOUTUBE_UPLOADS","true")
    monkeypatch.setenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","true")
    c,media=setup_client(tmp_path)
    pubid,rid,previous=_add_scheduled_public_fixture(c,media,"reschedule.mp4")
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        raw.execute("INSERT INTO settings(key,value,updated_at) VALUES('youtube_uploads_enabled','true',?) ON CONFLICT(key) DO UPDATE SET value='true',updated_at=excluded.updated_at",(datetime.now(timezone.utc).isoformat(),))
        raw.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at) SELECT ?, 'UPLOAD_YOUTUBE','QUEUED',source_id,media_id,?,?,? FROM published_videos WHERE id=?",
                    ("reschedule-upload",json.dumps({"published_video_id":pubid,"publish_at":previous,"publication_mode":"scheduled_public","tags":[]}),datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc).isoformat(),pubid))
    next_time=(datetime.now(timezone.utc)+timedelta(days=2)).isoformat()
    response=c.post(f"/api/publishing/videos/{pubid}/reschedule",json={"publish_at":next_time})
    assert response.status_code==200,response.text
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        db_schedule=raw.execute("SELECT scheduled_publish_at FROM published_videos WHERE id=?",(pubid,)).fetchone()[0]
        upload=json.loads(raw.execute("SELECT payload_json FROM jobs WHERE id='reschedule-upload'").fetchone()[0])
    assert datetime.fromisoformat(db_schedule.replace("Z","+00:00"))>datetime.now(timezone.utc)
    assert upload["publication_mode"]=="scheduled_public"
    assert upload["publish_at"] is not None
    c.__exit__(None,None,None)

def test_reconcile_public_video_is_forced_private_when_public_gate_is_disabled(tmp_path,monkeypatch):
    import app.main as main_module
    monkeypatch.setattr(main_module,"connected_status",lambda:{"connected":True,"channel_title":"Synthetic CI"})
    monkeypatch.delenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING",raising=False)
    monkeypatch.delenv("GCF_ENABLE_YOUTUBE_UPLOADS",raising=False)
    c,media=setup_client(tmp_path)
    pubid,render_id,future=_add_scheduled_public_fixture(c,media,"unexpected-public.mp4")
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        raw.execute("UPDATE published_videos SET publishing_status='RECONCILE_REQUIRED',youtube_video_id=? WHERE id=?",
                    ("yt-id-public",pubid))
    class Request:
        def __init__(self,result): self.result=result
        def execute(self): return self.result
    class Channels:
        def list(self,**kwargs): return Request({"items":[{"id":"connected-channel"}]})
    class Videos:
        def __init__(self): self.updates=[]
        def list(self,**kwargs):
            assert kwargs["id"]=="yt-id-public"
            return Request({"items":[{"id":"yt-id-public","snippet":{"channelId":"connected-channel"},"status":{"privacyStatus":"public"},"processingDetails":{"processingStatus":"succeeded"}}]})
        def update(self,**kwargs):
            self.updates.append(kwargs)
            return Request({"id":"yt-id-public","status":{"privacyStatus":"private"}})
    class Service:
        def __init__(self): self.channel_api=Channels(); self.video_api=Videos()
        def channels(self): return self.channel_api
        def videos(self): return self.video_api
    service=Service()
    monkeypatch.setattr(main_module,"youtube_service",lambda:service)
    response=c.post(f"/api/publishing/videos/{pubid}/reconcile",json={"youtube_video_id":"yt-id-public"})
    assert response.status_code==200,response.text
    assert response.json()["remote_privacy_status"]=="private"
    assert response.json()["local_status"]=="UPLOADED_PRIVATE"
    assert len(service.video_api.updates)==1
    assert service.video_api.updates[0]["body"]["status"]["privacyStatus"]=="private"
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        row=raw.execute("SELECT publishing_status,youtube_video_id,scheduled_publish_at FROM published_videos WHERE id=?",(pubid,)).fetchone()
    assert row==("UPLOADED_PRIVATE","yt-id-public",None)
    c.__exit__(None,None,None)

def test_rendered_output_can_have_only_one_publication_record(tmp_path):
    c,media=setup_client(tmp_path)
    pubid,rid,future=_add_scheduled_public_fixture(c,media,"unique-output.mp4")
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("""INSERT INTO published_videos(id,source_id,media_id,candidate_id,output_path,title,description,template_id,qc_status,publishing_status,created_at,updated_at,rendered_output_id)
                SELECT 'duplicate-output-pub',source_id,media_id,candidate_id,output_path,'Duplicate','','test','PASS','UPLOAD_PENDING',created_at,updated_at,rendered_output_id
                FROM published_videos WHERE id=?""",(pubid,))
    c.__exit__(None,None,None)

def test_youtube_analytics_schedule_requires_connected_oauth(tmp_path,monkeypatch):
    import app.main as main_module
    monkeypatch.setattr(main_module,"connected_status",lambda:{"connected":False,"reason":"no connected test channel"})
    c,_=setup_client(tmp_path)
    response=c.put("/api/youtube/analytics/schedule",json={"enabled":True,"interval_minutes":720,"window_days":7})
    assert response.status_code==503,response.text
    status=c.get("/api/youtube/analytics/schedule").json()
    assert status["enabled"] is False
    assert status["interval_minutes"]==1440
    assert status["window_days"]==7
    c.__exit__(None,None,None)

def test_youtube_analytics_schedule_persists_configuration(tmp_path,monkeypatch):
    import app.main as main_module
    monkeypatch.setattr(main_module,"connected_status",lambda:{"connected":True,"channel_title":"Test channel","reason":None})
    c,_=setup_client(tmp_path)
    response=c.put("/api/youtube/analytics/schedule",json={"enabled":True,"interval_minutes":720,"window_days":14})
    assert response.status_code==200,response.text
    status=c.get("/api/youtube/analytics/schedule").json()
    assert status["enabled"] is True
    assert status["interval_minutes"]==720
    assert status["window_days"]==14
    assert status["next_run"] is not None
    c.__exit__(None,None,None)

def test_analytics_summary_compares_same_window_and_requires_three_videos(tmp_path):
    c,_=setup_client(tmp_path)
    db_path=c.app.state.settings.database_path
    stamp="2026-10-20T12:00:00+00:00"
    fixtures=[
        ("analytics-source-1","analytics-media-1","analytics-candidate-1","analytics-pub-1","yt-analytics-1","reaction_moment",100,80),
        ("analytics-source-2","analytics-media-2","analytics-candidate-2","analytics-pub-2","yt-analytics-2","reaction_moment",200,70),
        ("analytics-source-3","analytics-media-3","analytics-candidate-3","analytics-pub-3","yt-analytics-3","reaction_moment",300,60),
        ("analytics-source-4","analytics-media-4","analytics-candidate-4","analytics-pub-4","yt-analytics-4","clutch_candidate",999,99),
    ]
    with sqlite3.connect(db_path) as raw:
        for sid,mid,cid,pid,vid,fmt,views,avg_pct in fixtures:
            raw.execute("INSERT INTO sources(id,source_url,platform,title,creator,game,discovered_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (sid,f"https://example.com/{sid}","manual","Fixture title","Owner","Valorant",stamp,stamp))
            raw.execute("INSERT INTO media_assets(id,source_id,original_path,file_name,file_size,sha256,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (mid,sid,f"/{mid}.mp4",f"{mid}.mp4",10,mid,"IMPORTED",stamp))
            raw.execute("INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (cid,mid,0,5,"test","fixture","RENDERED_QC_PASSED",stamp))
            raw.execute("""INSERT INTO content_plans(id,candidate_id,chosen_format,title,strategy_version,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",(f"plan-{cid}",cid,fmt,"Draft title","test-v1","DRAFT",stamp,stamp))
            raw.execute("""INSERT INTO published_videos(id,source_id,media_id,candidate_id,output_path,title,description,template_id,qc_status,publishing_status,youtube_video_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(pid,sid,mid,cid,f"/{pid}.mp4","Fixture video","", "test","PASS","UPLOADED_PRIVATE",vid,stamp,stamp))
            window={"start_date":"2026-10-12","end_date":"2026-10-18","metrics":{"video":vid,"views":views,"likes":2,"comments":1,"averageViewPercentage":avg_pct}}
            if sid=="analytics-source-4":
                window={"start_date":"2026-10-11","end_date":"2026-10-17","metrics":{"video":vid,"views":views,"likes":2,"comments":1,"averageViewPercentage":avg_pct}}
            raw.execute("INSERT INTO analytics_snapshots(id,published_video_id,captured_at,metrics_json,provider) VALUES(?,?,?,?,?)",
                (f"snapshot-{pid}",pid,stamp,json.dumps(window),"youtube_analytics_api"))
    response=c.get("/api/youtube/analytics/summary?start_date=2026-10-12&end_date=2026-10-18")
    assert response.status_code==200,response.text
    report=response.json()
    assert report["matched_video_count"]==3
    assert report["recommendation_status"]=="COMPARABLE_SAMPLE"
    assert report["best_supported_group"]["game"]=="Valorant"
    assert report["best_supported_group"]["format"]=="reaction_moment"
    assert report["best_supported_group"]["sample_size"]==3
    assert report["best_supported_group"]["views_total"]==600
    assert report["best_supported_group"]["averageViewPercentage_mean"]==70
    assert "causality" in report["recommendation_note"]
    c.__exit__(None,None,None)

def test_analytics_summary_does_not_recommend_with_insufficient_sample(tmp_path):
    c,_=setup_client(tmp_path)
    db_path=c.app.state.settings.database_path
    stamp="2026-10-20T12:00:00+00:00"
    sid="small-sample-source"; mid="small-sample-media"; cid="small-sample-candidate"; pid="small-sample-pub"; vid="small-sample-youtube"
    with sqlite3.connect(db_path) as raw:
        raw.execute("INSERT INTO sources(id,source_url,platform,title,creator,game,discovered_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (sid,"https://example.com/small-sample","manual","Fixture","Owner","Valorant",stamp,stamp))
        raw.execute("INSERT INTO media_assets(id,source_id,original_path,file_name,file_size,sha256,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (mid,sid,"/small.mp4","small.mp4",10,"small-hash","IMPORTED",stamp))
        raw.execute("INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (cid,mid,0,5,"test","fixture","RENDERED_QC_PASSED",stamp))
        raw.execute("""INSERT INTO content_plans(id,candidate_id,chosen_format,title,strategy_version,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)""",("small-plan",cid,"educational_breakdown","Draft","test-v1","DRAFT",stamp,stamp))
        raw.execute("""INSERT INTO published_videos(id,source_id,media_id,candidate_id,output_path,title,description,template_id,qc_status,publishing_status,youtube_video_id,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(pid,sid,mid,cid,"/small-pub.mp4","Fixture","", "test","PASS","UPLOADED_PRIVATE",vid,stamp,stamp))
        raw.execute("INSERT INTO analytics_snapshots(id,published_video_id,captured_at,metrics_json,provider) VALUES(?,?,?,?,?)",
            ("small-snapshot",pid,stamp,json.dumps({"start_date":"2026-10-12","end_date":"2026-10-18","metrics":{"video":vid,"views":12,"averageViewPercentage":91}}),"youtube_analytics_api"))
    report=c.get("/api/youtube/analytics/summary?start_date=2026-10-12&end_date=2026-10-18").json()
    assert report["matched_video_count"]==1
    assert report["recommendation_status"]=="INSUFFICIENT_SAMPLE"
    assert report["best_supported_group"] is None
    assert report["groups"][0]["minimum_sample_met"] is False
    c.__exit__(None,None,None)

def test_analytics_summary_rejects_malformed_or_incomplete_date_windows(tmp_path):
    c,_=setup_client(tmp_path)
    assert c.get("/api/youtube/analytics/summary?start_date=not-a-date").status_code==422
    assert c.get("/api/youtube/analytics/summary?start_date=2026-10-20&end_date=2026-10-19").status_code==422
    utc_today=datetime.now(timezone.utc).date().isoformat()
    assert c.get(f"/api/youtube/analytics/summary?end_date={utc_today}").status_code==422
    c.__exit__(None,None,None)

def test_candidate_caption_edits_round_trip_as_candidate_relative_times(tmp_path):
    c,media=setup_client(tmp_path)
    sid=add_source(c)
    assert authorize(c,sid).status_code==200
    media.mkdir(parents=True,exist_ok=True)
    (media/"caption-source.mp4").write_bytes(b"authorized fixture bytes")
    imported=c.post("/api/media/import",json={"source_id":sid,"path":"caption-source.mp4"})
    assert imported.status_code==201,imported.text
    mid=imported.json()["id"]
    cid="caption-edit-candidate"
    stamp="2026-10-20T00:00:00+00:00"
    db_path=c.app.state.settings.database_path
    with sqlite3.connect(db_path) as raw:
        raw.execute("INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (cid,mid,10.0,20.0,"test","caption test","NEEDS_REVIEW",stamp))
        raw.execute("INSERT INTO caption_segments(id,candidate_id,segment_index,start_seconds,end_seconds,text,confidence,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("caption-original",cid,0,11.0,13.0,"Original phrase",0.9,stamp))
    before=next(x for x in c.get("/api/candidates").json() if x["id"]==cid)
    assert before["caption_segments"][0]["start_seconds"]==1.0
    assert before["caption_segments"][0]["end_seconds"]==3.0
    edited=c.put(f"/api/candidates/{cid}/captions",json={"segments":[
        {"start_seconds":0.5,"end_seconds":1.5,"text":"Corrected first line"},
        {"start_seconds":2.0,"end_seconds":4.0,"text":"Second line"}
    ]})
    assert edited.status_code==200,edited.text
    after=next(x for x in c.get("/api/candidates").json() if x["id"]==cid)
    assert after["caption_segment_count"]==2
    assert after["caption_segments"][0]["text"]=="Corrected first line"
    assert after["caption_segments"][0]["start_seconds"]==0.5
    assert after["caption_segments"][1]["start_seconds"]==2.0
    with sqlite3.connect(db_path) as raw:
        rows=raw.execute("SELECT start_seconds,end_seconds,text FROM caption_segments WHERE candidate_id=? ORDER BY segment_index",(cid,)).fetchall()
    assert rows==[(10.5,11.5,"Corrected first line"),(12.0,14.0,"Second line")]
    overlap=c.put(f"/api/candidates/{cid}/captions",json={"segments":[
        {"start_seconds":0.0,"end_seconds":2.0,"text":"Overlaps"},
        {"start_seconds":1.0,"end_seconds":3.0,"text":"Second"}
    ]})
    assert overlap.status_code==422
    c.__exit__(None,None,None)

def test_candidate_caption_edit_requires_current_rights_and_review_state(tmp_path):
    c,media=setup_client(tmp_path); sid=add_source(c); authorize(c,sid); media.mkdir(parents=True,exist_ok=True)
    (media/"caption-state.mp4").write_bytes(b"authorized fixture bytes")
    imported=c.post("/api/media/import",json={"source_id":sid,"path":"caption-state.mp4"})
    assert imported.status_code==201,imported.text
    mid=imported.json()["id"]; cid="caption-state-candidate"; stamp="2026-10-20T00:00:00+00:00"
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        raw.execute("INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (cid,mid,0.0,5.0,"test","state test","APPROVED",stamp))
    response=c.put(f"/api/candidates/{cid}/captions",json={"segments":[{"start_seconds":0,"end_seconds":1,"text":"Caption"}]})
    assert response.status_code==409
    c.__exit__(None,None,None)

def test_candidate_render_profile_validation():
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"backend"))
    from app.main import CandidateDecisionIn
    assert CandidateDecisionIn(decision="approve").render_profile=="fit_pad"
    assert CandidateDecisionIn(decision="approve",render_profile="blur_fill").render_profile=="blur_fill"
    with pytest.raises(Exception):
        CandidateDecisionIn(decision="approve",render_profile="crop_fill")

def test_migration_prefixes_are_unique():
    from pathlib import Path
    migrations=ROOT/"backend"/"app"/"migrations"
    versions=[int(path.name.split("_",1)[0]) for path in migrations.glob("[0-9][0-9][0-9]_*.sql")]
    assert len(versions)==len(set(versions)),f"Duplicate migration version prefixes: {versions}"

def test_migration_files_have_unique_versions():
    from pathlib import Path
    migration_dir=Path(__file__).resolve().parents[1]/"backend"/"app"/"migrations"
    versions=[int(path.name.split("_",1)[0]) for path in migration_dir.glob("[0-9][0-9][0-9]_*.sql")]
    assert len(versions)==len(set(versions)),f"Duplicate migration versions: {versions}"
    assert sorted(versions)==list(range(1,max(versions)+1))

def test_failed_migration_rolls_back_schema_and_version_marker(tmp_path,monkeypatch):
    import app.db as database
    migrations=tmp_path/"migrations"; migrations.mkdir()
    (migrations/"001_bad.sql").write_text("CREATE TABLE partial_migration_table(id INTEGER);\\nTHIS IS NOT SQL;\\n",encoding="utf-8")
    monkeypatch.setattr(database,"MIGRATIONS_DIR",migrations)
    path=tmp_path/"broken.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        database.initialize(path)
    with sqlite3.connect(path) as raw:
        tables={row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        versions={row[0] for row in raw.execute("SELECT version FROM schema_migrations")}
    assert "partial_migration_table" not in tables
    assert 1 not in versions

def test_duplicate_migration_versions_fail_before_applying(tmp_path,monkeypatch):
    import app.db as database
    migrations=tmp_path/"migrations"; migrations.mkdir()
    (migrations/"001_first.sql").write_text("CREATE TABLE first_table(id INTEGER);",encoding="utf-8")
    (migrations/"001_second.sql").write_text("CREATE TABLE second_table(id INTEGER);",encoding="utf-8")
    monkeypatch.setattr(database,"MIGRATIONS_DIR",migrations)
    path=tmp_path/"duplicate.sqlite3"
    with pytest.raises(RuntimeError,match="Duplicate SQL migration version"):
        database.initialize(path)
    with sqlite3.connect(path) as raw:
        tables={row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "first_table" not in tables and "second_table" not in tables

def test_folder_watch_requires_current_authorized_source(tmp_path):
    c,media=setup_client(tmp_path)
    sid=add_source(c)
    (media/"incoming").mkdir(parents=True)
    response=c.post("/api/automation/folder-watches",json={"source_id":sid,"relative_path":"incoming"})
    assert response.status_code==409,response.text
    c.__exit__(None,None,None)

def test_folder_watch_rejects_paths_that_escape_media_root(tmp_path):
    c,_=setup_client(tmp_path)
    sid=add_source(c)
    authorize(c,sid)
    response=c.post("/api/automation/folder-watches",json={"source_id":sid,"relative_path":"../outside"})
    assert response.status_code==422,response.text
    c.__exit__(None,None,None)

def test_folder_watch_automatically_imports_stable_authorized_media(tmp_path,monkeypatch):
    from app.worker import Worker
    monkeypatch.setenv("GCF_WATCH_STABLE_SECONDS","5")
    c,media=setup_client(tmp_path)
    sid=add_source(c)
    assert authorize(c,sid).status_code==200
    folder=media/"incoming"
    folder.mkdir(parents=True)
    video=folder/"test-gameplay.mp4"
    video.write_bytes(b"authorized synthetic watched media")
    watch=c.post("/api/automation/folder-watches",json={
        "source_id":sid,"relative_path":"incoming","enabled":True,
        "recursive":False,"auto_analyze":True,"scan_interval_seconds":15})
    assert watch.status_code==201,watch.text
    watch_id=watch.json()["id"]
    worker=Worker(c.app.state.settings.database_path,None,None,5,media_root=media)
    # First scan records the stable file signature; it does not import files still being copied.
    first=worker.scan_folder_watch(watch_id)
    assert first["files_seen"]==1 and first["imported"]==0 and first["pending_stability"]==1
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        old=(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()
        raw.execute("UPDATE watched_media_files SET stable_since=? WHERE watch_id=?",(old,watch_id))
    # A subsequent scan after the stable interval imports and queues analysis without running FFmpeg in the API.
    second=worker.scan_folder_watch(watch_id)
    assert second["imported"]==1
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        media_row=raw.execute("SELECT id,source_id,original_path,state FROM media_assets").fetchone()
        job=raw.execute("SELECT job_type,status,media_id FROM jobs WHERE media_id=?",(media_row[0],)).fetchone()
        watch_file=raw.execute("SELECT status,media_id FROM watched_media_files WHERE watch_id=?",(watch_id,)).fetchone()
    assert media_row[1]==sid and Path(media_row[2])==video.resolve() and media_row[3]=="IMPORTED"
    assert tuple(job)==("ANALYZE_MEDIA","QUEUED",media_row[0])
    assert watch_file[0]=="IMPORTED" and watch_file[1]==media_row[0]
    c.__exit__(None,None,None)

def test_folder_watch_rechecks_rights_after_file_becomes_stable(tmp_path):
    from app.worker import Worker
    c,media=setup_client(tmp_path)
    sid=add_source(c)
    assert authorize(c,sid).status_code==200
    folder=media/"incoming";folder.mkdir(parents=True)
    (folder/"clip.mp4").write_bytes(b"rights-revocation fixture")
    watch=c.post("/api/automation/folder-watches",json={"source_id":sid,"relative_path":"incoming","scan_interval_seconds":15})
    assert watch.status_code==201,watch.text
    watch_id=watch.json()["id"]
    worker=Worker(c.app.state.settings.database_path,None,None,5,media_root=media)
    assert worker.scan_folder_watch(watch_id)["pending_stability"]==1
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        old=(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()
        raw.execute("UPDATE watched_media_files SET stable_since=? WHERE watch_id=?",(old,watch_id))
        raw.execute("UPDATE source_permissions SET status='REVOKED' WHERE source_id=?",(sid,))
    result=worker.scan_folder_watch(watch_id)
    assert result["blocked_rights"]==1 and result["imported"]==0
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        assert raw.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0]==0
        assert raw.execute("SELECT status FROM watched_media_files WHERE watch_id=?",(watch_id,)).fetchone()[0]=="SKIPPED_RIGHTS"
    c.__exit__(None,None,None)

def test_folder_watch_reimports_replaced_file_after_stability(tmp_path):
    from app.worker import Worker
    c,media=setup_client(tmp_path); sid=add_source(c); assert authorize(c,sid).status_code==200
    folder=media/"incoming";folder.mkdir(parents=True)
    path=folder/"replace.mp4";path.write_bytes(b"original clip bytes")
    watch=c.post("/api/automation/folder-watches",json={"source_id":sid,"relative_path":"incoming","scan_interval_seconds":15})
    assert watch.status_code==201,watch.text
    watch_id=watch.json()["id"]
    worker=Worker(c.app.state.settings.database_path,None,None,5,media_root=media)
    worker.scan_folder_watch(watch_id)
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        old=(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()
        raw.execute("UPDATE watched_media_files SET stable_since=? WHERE watch_id=?",(old,watch_id))
    assert worker.scan_folder_watch(watch_id)["imported"]==1
    path.write_bytes(b"replacement clip has different size")
    changed=worker.scan_folder_watch(watch_id)
    assert changed["imported"]==0 and changed["pending_stability"]==1
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        old=(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()
        raw.execute("UPDATE watched_media_files SET stable_since=? WHERE watch_id=?",(old,watch_id))
    replacement=worker.scan_folder_watch(watch_id)
    assert replacement["imported"]==1
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        assert raw.execute("SELECT COUNT(*) FROM media_assets WHERE source_id=?",(sid,)).fetchone()[0]==2
    c.__exit__(None,None,None)

def test_folder_watch_prevents_overlapping_source_mappings(tmp_path):
    c,media=setup_client(tmp_path)
    sid1=add_source(c)
    # Register a second distinct source URL for the second mapping.
    second=c.post("/api/sources",json={"source_url":"https://example.com/other-game","platform":"manual","title":"Other game"})
    assert second.status_code==201
    sid2=second.json()["id"]
    assert authorize(c,sid1).status_code==200 and authorize(c,sid2).status_code==200
    first=c.post("/api/automation/folder-watches",json={"source_id":sid1,"relative_path":"incoming"})
    assert first.status_code==201,first.text
    overlap=c.post("/api/automation/folder-watches",json={"source_id":sid2,"relative_path":"incoming/another"})
    assert overlap.status_code==409,overlap.text
    c.__exit__(None,None,None)

def test_folder_watch_update_cannot_enable_overlap(tmp_path):
    c,_=setup_client(tmp_path)
    sid1=add_source(c)
    s2=c.post("/api/sources",json={"source_url":"https://example.com/second","platform":"manual","title":"Second"})
    assert s2.status_code==201
    sid2=s2.json()["id"]
    assert authorize(c,sid1).status_code==200 and authorize(c,sid2).status_code==200
    first=c.post("/api/automation/folder-watches",json={"source_id":sid1,"relative_path":"incoming"})
    assert first.status_code==201,first.text
    second=c.post("/api/automation/folder-watches",json={"source_id":sid2,"relative_path":"incoming/subfolder","enabled":False})
    assert second.status_code==409,second.text
    c.__exit__(None,None,None)

def test_folder_watch_can_be_disabled_without_deleting_imported_media(tmp_path):
    c,media=setup_client(tmp_path)
    sid=add_source(c)
    assert authorize(c,sid).status_code==200
    watch=c.post("/api/automation/folder-watches",json={"source_id":sid,"relative_path":"incoming"})
    assert watch.status_code==201,watch.text
    wid=watch.json()["id"]
    updated=c.put(f"/api/automation/folder-watches/{wid}",json={"enabled":False})
    assert updated.status_code==200 and updated.json()["enabled"] is False
    assert c.get("/api/automation/folder-watches").json()[0]["enabled"] is False
    deleted=c.delete(f"/api/automation/folder-watches/{wid}")
    assert deleted.status_code==200 and deleted.json()["media_retained"] is True
    assert c.get("/api/automation/folder-watches").json()==[]
    c.__exit__(None,None,None)


def test_narration_status_is_reported_without_exposing_paths(tmp_path,monkeypatch):
    monkeypatch.setenv("GCF_ENABLE_NARRATION","false")
    monkeypatch.delenv("GCF_PIPER_EXECUTABLE_PATH",raising=False)
    monkeypatch.delenv("GCF_PIPER_MODEL_PATH",raising=False)
    monkeypatch.delenv("GCF_PIPER_CONFIG_PATH",raising=False)
    c,_=setup_client(tmp_path)
    response=c.get("/api/integrations/status")
    assert response.status_code==200,response.text
    narration=response.json()["narration"]
    assert narration["enabled"] is False
    assert narration["ready"] is False
    assert narration["network_calls"] is False
    assert narration["model_downloads"] is False
    c.__exit__(None,None,None)


def test_content_plan_edit_saves_owner_reviewed_script_and_narration_is_opt_in(tmp_path,monkeypatch):
    monkeypatch.setenv("GCF_ENABLE_NARRATION","false")
    c,media=setup_client(tmp_path)
    sid=add_source(c)
    assert authorize(c,sid).status_code==200
    media.mkdir(parents=True,exist_ok=True)
    (media/"narration-clip.mp4").write_bytes(b"synthetic registry-only media bytes")
    imported=c.post("/api/media/import",json={"source_id":sid,"path":"narration-clip.mp4"})
    assert imported.status_code==201,imported.text
    mid=imported.json()["id"]
    cid="narration-content-plan-test"
    stamp="2026-10-01T00:00:00+00:00"
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        raw.execute("""INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",(cid,mid,1.0,8.0,"test","Voiceover script test","NEEDS_REVIEW",stamp))
    saved=c.put(f"/api/candidates/{cid}/content-plan",json={
        "title":"Valorant | A clean round",
        "description":"Owner-reviewed test description.",
        "tags":["Valorant","shorts"],
        "narration_text":"Hold the angle. Wait for the sound, then rotate."
    })
    assert saved.status_code==200,saved.text
    assert saved.json()["narration_text"]=="Hold the angle. Wait for the sound, then rotate."
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        raw.row_factory=sqlite3.Row
        plan=raw.execute("SELECT narration_text,narration_audio_status,narration_audio_path FROM content_plans WHERE candidate_id=?",(cid,)).fetchone()
    assert plan["narration_text"]=="Hold the angle. Wait for the sound, then rotate."
    assert plan["narration_audio_status"]=="NOT_GENERATED"
    assert plan["narration_audio_path"] is None
    blocked=c.post(f"/api/candidates/{cid}/narration")
    assert blocked.status_code==503,blocked.text
    c.__exit__(None,None,None)


def test_editing_narration_script_invalidates_previous_audio(tmp_path,monkeypatch):
    c,media=setup_client(tmp_path)
    sid=add_source(c)
    assert authorize(c,sid).status_code==200
    media.mkdir(parents=True,exist_ok=True)
    (media/"narration-reset.mp4").write_bytes(b"synthetic registry-only media bytes")
    imported=c.post("/api/media/import",json={"source_id":sid,"path":"narration-reset.mp4"})
    assert imported.status_code==201,imported.text
    mid=imported.json()["id"]
    cid="narration-invalidates-audio"
    stamp="2026-10-01T00:00:00+00:00"
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        raw.execute("""INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",(cid,mid,0.0,10.0,"test","Narration invalidation test","NEEDS_REVIEW",stamp))
        raw.execute("""INSERT INTO content_plans(id,candidate_id,chosen_format,title,narration_text,created_at,updated_at,
            narration_audio_path,narration_audio_sha256,narration_audio_duration_seconds,narration_audio_status,narration_audio_error,narration_audio_generated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",("plan-narration-reset",cid,"gameplay_highlight","Test title","First script line.",stamp,stamp,
            "data/outputs/old-voice.wav","abc123",1.5,"READY","","2026-10-01T00:00:00+00:00"))
    saved=c.put(f"/api/candidates/{cid}/content-plan",json={"narration_text":"A new script line."})
    assert saved.status_code==200,saved.text
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        raw.row_factory=sqlite3.Row
        plan=raw.execute("SELECT narration_text,narration_audio_status,narration_audio_path,narration_audio_sha256,narration_audio_duration_seconds FROM content_plans WHERE candidate_id=?",(cid,)).fetchone()
    assert plan["narration_text"]=="A new script line."
    assert plan["narration_audio_status"]=="NOT_GENERATED"
    assert plan["narration_audio_path"] is None
    assert plan["narration_audio_sha256"] is None
    assert plan["narration_audio_duration_seconds"] is None
    c.__exit__(None,None,None)


def test_local_script_drafting_is_opt_in(tmp_path,monkeypatch):
    monkeypatch.delenv("GCF_ENABLE_SCRIPT_DRAFTING",raising=False)
    c,_=setup_client(tmp_path)
    response=c.post("/api/candidates/not-a-real-candidate/narration/draft")
    assert response.status_code==503
    c.__exit__(None,None,None)

def test_script_drafting_requires_candidate_evidence(tmp_path,monkeypatch):
    monkeypatch.setenv("GCF_ENABLE_SCRIPT_DRAFTING","true")
    monkeypatch.setenv("GCF_SCRIPT_DRAFTER_BASE_URL","http://127.0.0.1:11434/api/chat")
    monkeypatch.setenv("GCF_SCRIPT_DRAFTER_MODEL","test-model")
    c,media=setup_client(tmp_path); sid=add_source(c); assert authorize(c,sid).status_code==200
    media.mkdir(parents=True,exist_ok=True); (media/"clip.mp4").write_bytes(b"test media bytes")
    imported=c.post("/api/media/import",json={"source_id":sid,"path":"clip.mp4"}); assert imported.status_code==201,imported.text
    cid="script-draft-no-evidence"
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        raw.execute("INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (cid,imported.json()["id"],0.0,5.0,"test","No evidence candidate","NEEDS_REVIEW","2026-10-01T00:00:00+00:00"))
    response=c.post(f"/api/candidates/{cid}/narration/draft")
    assert response.status_code==409,response.text
    c.__exit__(None,None,None)

def test_script_drafting_queues_only_for_authorized_candidate_with_transcript(tmp_path,monkeypatch):
    monkeypatch.setenv("GCF_ENABLE_SCRIPT_DRAFTING","true")
    monkeypatch.setenv("GCF_SCRIPT_DRAFTER_BASE_URL","http://127.0.0.1:11434/api/chat")
    monkeypatch.setenv("GCF_SCRIPT_DRAFTER_MODEL","test-model")
    c,media=setup_client(tmp_path); sid=add_source(c); assert authorize(c,sid).status_code==200
    media.mkdir(parents=True,exist_ok=True); (media/"clip.mp4").write_bytes(b"test media bytes")
    imported=c.post("/api/media/import",json={"source_id":sid,"path":"clip.mp4"}); assert imported.status_code==201,imported.text
    mid=imported.json()["id"]; cid="script-draft-with-evidence"; stamp="2026-10-01T00:00:00+00:00"
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        raw.execute("INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (cid,mid,0.0,5.0,"test","Transcript evidence candidate","NEEDS_REVIEW",stamp))
        raw.execute("""INSERT INTO transcripts(id,media_id,language,language_probability,full_text,model_name,status,error,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,'COMPLETED','',?,?)""",("draft-api-transcript",mid,"en",0.95,"Watch this angle","test",stamp,stamp))
        raw.execute("INSERT INTO transcript_segments(id,transcript_id,segment_index,start_seconds,end_seconds,text,confidence) VALUES(?,?,?,?,?,?,?)",
                    ("draft-api-segment","draft-api-transcript",0,1.0,2.5,"Watch this angle",0.9))
    response=c.post(f"/api/candidates/{cid}/narration/draft")
    assert response.status_code==202,response.text
    assert response.json()["owner_review_required"] is True
    assert response.json()["automatic_synthesis"] is False
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        row=raw.execute("SELECT job_type,status FROM jobs WHERE id=?",(response.json()["id"],)).fetchone()
    assert tuple(row)==("DRAFT_NARRATION_SCRIPT","QUEUED")
    c.__exit__(None,None,None)

def test_voice_synthesis_requires_explicit_script_review(tmp_path,monkeypatch):
    import app.main as main_module
    monkeypatch.setattr(main_module,"get_narration_status",lambda:{"ready":True,"reason":None})
    c,media=setup_client(tmp_path); sid=add_source(c); assert authorize(c,sid).status_code==200
    media.mkdir(parents=True,exist_ok=True); (media/"clip.mp4").write_bytes(b"test media bytes")
    imported=c.post("/api/media/import",json={"source_id":sid,"path":"clip.mp4"}); assert imported.status_code==201,imported.text
    mid=imported.json()["id"]; cid="script-review-gate"; stamp="2026-10-01T00:00:00+00:00"
    with sqlite3.connect(c.app.state.settings.database_path) as raw:
        raw.execute("INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (cid,mid,0.0,5.0,"test","Voice script review","NEEDS_REVIEW",stamp))
    saved=c.put(f"/api/candidates/{cid}/content-plan",json={"title":"Test voiceover","narration_text":"Watch this angle, then reset."})
    assert saved.status_code==200,saved.text
    blocked=c.post(f"/api/candidates/{cid}/narration")
    assert blocked.status_code==409,blocked.text
    approved=c.put(f"/api/candidates/{cid}/content-plan",json={"narration_text":"Watch this angle, then reset.","narration_script_reviewed":True})
    assert approved.status_code==200,approved.text
    queued=c.post(f"/api/candidates/{cid}/narration")
    assert queued.status_code==202,queued.text
    assert queued.json()["status"]=="QUEUED"
    c.__exit__(None,None,None)


def _seed_youtube_processing_publication(client, media_root, tmp_path, published_id, scheduled=True):
    import hashlib
    sid=add_source(client)
    assert authorize(client,sid).status_code==200
    media_root.mkdir(parents=True,exist_ok=True)
    source_file=media_root/"gameplay.mp4"
    source_file.write_bytes(b"test fixture video bytes")
    imported=client.post("/api/media/import",json={"source_id":sid,"path":"gameplay.mp4"})
    assert imported.status_code==201,imported.text
    mid=imported.json()["id"]
    db=client.app.state.settings.database_path
    output_dir=db.parent/"outputs"
    output_dir.mkdir(parents=True,exist_ok=True)
    output=output_dir/(published_id+".mp4")
    output.write_bytes(b"private upload fixture")
    sha=hashlib.sha256(output.read_bytes()).hexdigest()
    candidate_id=published_id+"-candidate"
    rendered_id=published_id+"-render"
    stamp=datetime.now(timezone.utc).isoformat(timespec="seconds")
    publish_at=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(timespec="seconds") if scheduled else None
    with sqlite3.connect(db) as raw:
        raw.execute("INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (candidate_id,mid,0.0,8.0,"test","YouTube processing test","RENDERED_QC_PASSED",stamp))
        raw.execute("""INSERT INTO rendered_outputs(id,candidate_id,media_id,output_path,file_size,sha256,duration_seconds,width,height,video_codec,audio_codec,qc_status,qc_report_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (rendered_id,candidate_id,mid,str(output),output.stat().st_size,sha,8.0,1080,1920,"h264","aac","PASS","{}",stamp))
        raw.execute("""INSERT INTO published_videos(id,source_id,media_id,candidate_id,output_path,title,description,template_id,qc_status,publishing_status,youtube_video_id,created_at,updated_at,rendered_output_id,scheduled_publish_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (published_id,sid,mid,candidate_id,str(output),"Test upload","", "test","PASS","PROCESSING","youtube-video-123",stamp,stamp,rendered_id,publish_at))
        raw.execute("INSERT INTO settings(key,value,updated_at) VALUES('youtube_uploads_enabled','true',?) ON CONFLICT(key) DO UPDATE SET value='true',updated_at=excluded.updated_at",(stamp,))
        raw.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at) VALUES(?, 'CHECK_YOUTUBE_PROCESSING','RUNNING',?,?, ?,?,?)",
                    (published_id+"-check",sid,mid,json.dumps({"published_video_id":published_id,"poll_attempt":0,"max_polls":20}),stamp,stamp))
    return db, published_id+"-check", published_id, publish_at

def _fake_youtube_processing_service(processing_status, privacy_status="private"):
    class Request:
        def execute(self):
            return {"items":[{"id":"youtube-video-123","processingDetails":{"processingStatus":processing_status,"processingFailureReason":"test_failure" if processing_status=="failed" else ""},"status":{"privacyStatus":privacy_status}}]}
    class Videos:
        def list(self,**kwargs):
            assert kwargs.get("part")=="processingDetails,status"
            assert kwargs.get("id")=="youtube-video-123"
            return Request()
    class Service:
        def videos(self):
            return Videos()
    return Service()

def test_youtube_processing_success_queues_gated_publication(tmp_path,monkeypatch):
    import app.worker as worker_module
    from app.worker import Worker
    monkeypatch.setenv("GCF_ENABLE_YOUTUBE_UPLOADS","true")
    monkeypatch.setenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","true")
    monkeypatch.setattr(worker_module,"youtube_service",lambda:_fake_youtube_processing_service("succeeded"))
    client,media=setup_client(tmp_path)
    db,job_id,published_id,publish_at=_seed_youtube_processing_publication(client,media,tmp_path,"processing-success",scheduled=True)
    Worker(db,None,None,5).check_youtube_processing({"id":job_id,"payload_json":json.dumps({"published_video_id":published_id,"poll_attempt":0,"max_polls":20})})
    with sqlite3.connect(db) as raw:
        video=raw.execute("SELECT publishing_status,remote_processing_status,scheduled_publish_at FROM published_videos WHERE id=?",(published_id,)).fetchone()
        release=raw.execute("SELECT status,next_attempt_at,payload_json FROM jobs WHERE job_type='PUBLISH_YOUTUBE' AND payload_json LIKE ?",("%"+published_id+"%",)).fetchone()
    assert tuple(video[:2])==("SCHEDULED","succeeded")
    assert video[2]==publish_at
    assert release is not None
    assert release[0]=="QUEUED"
    assert datetime.fromisoformat(release[1].replace("Z","+00:00")) > datetime.now(timezone.utc)
    assert json.loads(release[2])["published_video_id"]==published_id
    client.__exit__(None,None,None)

def test_youtube_processing_failure_cancels_public_release(tmp_path,monkeypatch):
    import app.worker as worker_module
    from app.worker import Worker
    monkeypatch.setenv("GCF_ENABLE_YOUTUBE_UPLOADS","true")
    monkeypatch.setenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","true")
    monkeypatch.setattr(worker_module,"youtube_service",lambda:_fake_youtube_processing_service("failed"))
    client,media=setup_client(tmp_path)
    db,job_id,published_id,_=_seed_youtube_processing_publication(client,media,tmp_path,"processing-failure",scheduled=True)
    stamp=datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(db) as raw:
        raw.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at) VALUES(?, 'PUBLISH_YOUTUBE','QUEUED',?,?, ?,?,?)",
                    (published_id+"-release", "source-unused", "media-unused", json.dumps({"published_video_id":published_id,"publish_at":"2099-01-01T00:00:00+00:00"}),stamp,stamp))
    # Use actual foreign-key references for the scheduled job.
    with sqlite3.connect(db) as raw:
        raw.execute("UPDATE jobs SET source_id=(SELECT source_id FROM published_videos WHERE id=?),media_id=(SELECT media_id FROM published_videos WHERE id=?) WHERE id=?",
                    (published_id,published_id,published_id+"-release"))
    Worker(db,None,None,5).check_youtube_processing({"id":job_id,"payload_json":json.dumps({"published_video_id":published_id,"poll_attempt":0,"max_polls":20})})
    with sqlite3.connect(db) as raw:
        video=raw.execute("SELECT publishing_status,remote_processing_status,processing_failure_reason FROM published_videos WHERE id=?",(published_id,)).fetchone()
        release=raw.execute("SELECT status FROM jobs WHERE id=?",(published_id+"-release",)).fetchone()
    assert video[0]=="PROCESSING_FAILED"
    assert video[1]=="failed"
    assert "test_failure" in video[2]
    assert release[0]=="CANCELLED"
    client.__exit__(None,None,None)

def test_youtube_processing_success_keeps_video_private_when_public_gate_disabled(tmp_path,monkeypatch):
    import app.worker as worker_module
    from app.worker import Worker
    monkeypatch.setenv("GCF_ENABLE_YOUTUBE_UPLOADS","true")
    monkeypatch.setenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","false")
    monkeypatch.setattr(worker_module,"youtube_service",lambda:_fake_youtube_processing_service("succeeded"))
    client,media=setup_client(tmp_path)
    db,job_id,published_id,_=_seed_youtube_processing_publication(client,media,tmp_path,"processing-private",scheduled=True)
    Worker(db,None,None,5).check_youtube_processing({"id":job_id,"payload_json":json.dumps({"published_video_id":published_id,"poll_attempt":0,"max_polls":20})})
    with sqlite3.connect(db) as raw:
        video=raw.execute("SELECT publishing_status,remote_processing_status,scheduled_publish_at FROM published_videos WHERE id=?",(published_id,)).fetchone()
        releases=raw.execute("SELECT COUNT(*) FROM jobs WHERE job_type='PUBLISH_YOUTUBE' AND payload_json LIKE ? AND status IN ('QUEUED','RUNNING')",("%"+published_id+"%",)).fetchone()[0]
    assert tuple(video)==("READY_PRIVATE","succeeded",None)
    assert releases==0
    client.__exit__(None,None,None)


def test_scheduled_public_worker_waits_until_requested_time(tmp_path,monkeypatch):
    import app.worker as worker_module
    from app.worker import Worker
    monkeypatch.setenv("GCF_ENABLE_YOUTUBE_UPLOADS","true")
    monkeypatch.setenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","true")
    monkeypatch.setattr(worker_module,"youtube_service",lambda:(_ for _ in ()).throw(AssertionError("YouTube API must not be called before the scheduled time")))
    client,media=setup_client(tmp_path)
    db,job_id,published_id,publish_at=_seed_youtube_processing_publication(client,media,tmp_path,"public-not-due",scheduled=True)
    with sqlite3.connect(db) as raw:
        raw.execute("UPDATE jobs SET job_type='PUBLISH_YOUTUBE',payload_json=? WHERE id=?",
                    (json.dumps({"published_video_id":published_id,"publish_at":publish_at}),job_id))
    Worker(db,None,None,5).publish_youtube({"id":job_id,"payload_json":json.dumps({"published_video_id":published_id,"publish_at":publish_at})})
    with sqlite3.connect(db) as raw:
        status=raw.execute("SELECT status,next_attempt_at FROM jobs WHERE id=?",(job_id,)).fetchone()
        publication=raw.execute("SELECT publishing_status,scheduled_publish_at FROM published_videos WHERE id=?",(published_id,)).fetchone()
    assert status[0]=="QUEUED"
    assert datetime.fromisoformat(status[1].replace("Z","+00:00")) >= datetime.fromisoformat(publish_at.replace("Z","+00:00"))
    assert tuple(publication)==("PROCESSING",publish_at)
    client.__exit__(None,None,None)

def test_scheduled_public_worker_defers_while_youtube_still_processing(tmp_path,monkeypatch):
    import app.worker as worker_module
    from app.worker import Worker
    monkeypatch.setenv("GCF_ENABLE_YOUTUBE_UPLOADS","true")
    monkeypatch.setenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","true")
    service=_fake_youtube_processing_service("processing","private")
    monkeypatch.setattr(worker_module,"youtube_service",lambda:service)
    client,media=setup_client(tmp_path)
    db,job_id,published_id,_=_seed_youtube_processing_publication(client,media,tmp_path,"public-pending",scheduled=True)
    past=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(timespec="seconds")
    payload=json.dumps({"published_video_id":published_id,"publish_at":past})
    with sqlite3.connect(db) as raw:
        raw.execute("UPDATE published_videos SET publishing_status='SCHEDULED',scheduled_publish_at=? WHERE id=?",(past,published_id))
        raw.execute("UPDATE jobs SET job_type='PUBLISH_YOUTUBE',payload_json=? WHERE id=?",(payload,job_id))
    Worker(db,None,None,5).publish_youtube({"id":job_id,"payload_json":payload})
    with sqlite3.connect(db) as raw:
        publication=raw.execute("SELECT publishing_status,remote_processing_status FROM published_videos WHERE id=?",(published_id,)).fetchone()
        original_job=raw.execute("SELECT status FROM jobs WHERE id=?",(job_id,)).fetchone()
        recheck=raw.execute("SELECT status,next_attempt_at,payload_json FROM jobs WHERE job_type='CHECK_YOUTUBE_PROCESSING' AND id<>? AND payload_json LIKE ? ORDER BY created_at DESC LIMIT 1",(job_id,"%"+published_id+"%",)).fetchone()
    assert tuple(publication)==("PROCESSING","processing")
    assert original_job[0]=="COMPLETED"
    assert recheck is not None and recheck[0]=="QUEUED"
    assert json.loads(recheck[2])["published_video_id"]==published_id
    client.__exit__(None,None,None)


def test_low_disk_safety_pauses_queue_and_requires_manual_resume(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import app.worker as worker_module
    from app.worker import Worker
    monkeypatch.setenv("GCF_DISK_SAFETY_ENABLED","true")
    monkeypatch.setenv("GCF_MIN_FREE_DISK_GB","3")
    free={"bytes":2*(1024**3)}
    monkeypatch.setattr(worker_module.shutil,"disk_usage",lambda path:SimpleNamespace(total=100*(1024**3),used=100*(1024**3)-free["bytes"],free=free["bytes"]))
    client,_=setup_client(tmp_path)
    db=client.app.state.settings.database_path
    worker=Worker(db,None,None,5)
    assert worker.one() is False
    with sqlite3.connect(db) as raw:
        values={key:value for key,value in raw.execute("SELECT key,value FROM settings WHERE key IN ('global_paused','low_disk_pause_active','system_pause_reason','storage_safety_status')")}
    assert values["global_paused"]=="true"
    assert values["low_disk_pause_active"]=="true"
    assert values["system_pause_reason"]=="low_disk"
    assert values["storage_safety_status"]=="LOW"
    status=client.get("/api/operations/storage")
    assert status.status_code==200,status.text
    assert status.json()["disk_state"]=="LOW"
    assert status.json()["blocked"] is True
    assert status.json()["low_disk_pause_active"] is True
    free["bytes"]=10*(1024**3)
    assert worker.one() is False
    with sqlite3.connect(db) as raw:
        values={key:value for key,value in raw.execute("SELECT key,value FROM settings WHERE key IN ('global_paused','low_disk_pause_active','system_pause_reason','storage_safety_status')")}
    assert values["storage_safety_status"]=="OK"
    assert values["low_disk_pause_active"]=="false"
    assert values["global_paused"]=="true"
    assert values["system_pause_reason"]=="low_disk_recovered_waiting_for_manual_resume"
    resumed=client.post("/api/system/pause",json={"paused":False})
    assert resumed.status_code==200
    with sqlite3.connect(db) as raw:
        values={key:value for key,value in raw.execute("SELECT key,value FROM settings WHERE key IN ('global_paused','system_pause_reason')")}
    assert values["global_paused"]=="false"
    assert values["system_pause_reason"]==""
    client.__exit__(None,None,None)

def test_disk_capacity_check_failure_pauses_fail_closed(tmp_path,monkeypatch):
    import app.worker as worker_module
    from app.worker import Worker
    monkeypatch.setenv("GCF_DISK_SAFETY_ENABLED","true")
    monkeypatch.setenv("GCF_MIN_FREE_DISK_GB","3")
    def broken_usage(_path):
        raise OSError("permission denied")
    monkeypatch.setattr(worker_module.shutil,"disk_usage",broken_usage)
    client,_=setup_client(tmp_path)
    db=client.app.state.settings.database_path
    assert Worker(db,None,None,5).one() is False
    with sqlite3.connect(db) as raw:
        values={key:value for key,value in raw.execute("SELECT key,value FROM settings WHERE key IN ('global_paused','low_disk_pause_active','system_pause_reason','storage_safety_status')")}
    assert values["global_paused"]=="true"
    assert values["low_disk_pause_active"]=="true"
    assert values["system_pause_reason"]=="storage_check_failed"
    assert values["storage_safety_status"]=="ERROR"
    client.__exit__(None,None,None)
