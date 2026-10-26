from pathlib import Path
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
