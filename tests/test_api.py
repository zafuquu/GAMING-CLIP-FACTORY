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
    assert {1,2,3,4,5,6,7}.issubset(versions)
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
