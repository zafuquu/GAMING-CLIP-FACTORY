from pathlib import Path
from datetime import datetime, timedelta, timezone
import sqlite3
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
    assert {1,2,3,4}.issubset(versions)
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
