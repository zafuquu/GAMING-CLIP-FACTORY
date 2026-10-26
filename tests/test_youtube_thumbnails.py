from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import sqlite3
import sys

from fastapi.testclient import TestClient
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"))

from app.config import Settings
from app.main import create_app
from app.worker import Worker
import app.worker as worker_module


def fixture(tmp_path, monkeypatch, thumbnail_status="GENERATION_FAILED", has_thumbnail=False):
    monkeypatch.setenv("GCF_ENABLE_YOUTUBE_UPLOADS","true")
    monkeypatch.setenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING","false")
    root=tmp_path/"project"; data=root/"data"; media=data/"media"; outputs=data/"outputs"
    cfg=Settings(root,data,media,data/"factory.sqlite3",None,None,10)
    client=TestClient(create_app(settings=cfg,start_worker=False)); client.__enter__()
    source=client.post("/api/sources",json={
        "source_url":"https://example.com/thumbnail-test",
        "platform":"test","title":"Thumbnail test","game":"Valorant"
    })
    assert source.status_code==201,source.text
    sid=source.json()["id"]
    rights=client.put(f"/api/sources/{sid}/rights",json={
        "status":"AUTHORIZED","license_type":"Synthetic test permission",
        "permission_evidence":"Synthetic fixture created in CI",
        "editing_allowed":True,"commercial_use_allowed":True,"reviewed_by_owner":True
    })
    assert rights.status_code==200,rights.text
    media.mkdir(parents=True,exist_ok=True); outputs.mkdir(parents=True,exist_ok=True)
    source_path=media/"source.mp4"; source_path.write_bytes(b"authorized synthetic source bytes")
    imp=client.post("/api/media/import",json={"source_id":sid,"path":"source.mp4"})
    assert imp.status_code==201,imp.text
    mid=imp.json()["id"]
    output=outputs/"source-render.mp4"; output.write_bytes(b"synthetic QC passed rendered video")
    output_sha=hashlib.sha256(output.read_bytes()).hexdigest()
    poster=outputs/"source-poster.jpg"; Image.new("RGB",(360,640),(40,75,110)).save(poster,"JPEG",quality=80)
    candidate_id="candidate-thumb-test"; render_id="render-thumb-test"; pub_id="published-thumb-test"
    stamp=datetime.now(timezone.utc).isoformat(timespec="seconds")
    thumbnail_path=None; thumbnail_sha=None; width=None; height=None
    if has_thumbnail:
        cover=outputs/"cover.jpg"; Image.new("RGB",(1080,1920),(24,50,70)).save(cover,"JPEG",quality=80)
        thumbnail_path=str(cover); thumbnail_sha=hashlib.sha256(cover.read_bytes()).hexdigest(); width,height=1080,1920
    with sqlite3.connect(cfg.database_path) as db:
        db.execute("""INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",(candidate_id,mid,0,8,"test","synthetic candidate","RENDERED_QC_PASSED",stamp))
        db.execute("""INSERT INTO rendered_outputs(id,candidate_id,media_id,output_path,file_size,sha256,duration_seconds,width,height,video_codec,audio_codec,qc_status,qc_report_json,created_at,thumbnail_path,thumbnail_sha256,thumbnail_width,thumbnail_height,render_profile)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
            render_id,candidate_id,mid,str(output),output.stat().st_size,output_sha,8,1080,1920,"h264","aac","PASS",'{"passed":true}',stamp,
            str(poster),hashlib.sha256(poster.read_bytes()).hexdigest(),360,640,"fit_pad"))
        db.execute("""INSERT INTO published_videos(id,source_id,media_id,candidate_id,output_path,title,description,template_id,qc_status,publishing_status,youtube_video_id,created_at,updated_at,rendered_output_id,remote_processing_status,custom_thumbnail_path,custom_thumbnail_sha256,custom_thumbnail_width,custom_thumbnail_height,custom_thumbnail_status)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
            pub_id,sid,mid,candidate_id,str(output),"A clean round wins","Synthetic description","fit_pad","PASS","READY_PRIVATE",
            "youtube-video-123",stamp,stamp,render_id,"succeeded",thumbnail_path,thumbnail_sha,width,height,thumbnail_status))
        db.execute("INSERT INTO settings(key,value,updated_at) VALUES('youtube_uploads_enabled','true',?) ON CONFLICT(key) DO UPDATE SET value='true',updated_at=excluded.updated_at",(stamp,))
    return client,cfg,{"source_id":sid,"media_id":mid,"published_id":pub_id,"candidate_id":candidate_id,
        "render_id":render_id,"output":output,"poster":poster,"db":cfg.database_path}


def test_thumbnail_regeneration_worker_creates_cover_and_queues_official_upload(tmp_path,monkeypatch):
    client,cfg,ids=fixture(tmp_path,monkeypatch,thumbnail_status="GENERATION_FAILED",has_thumbnail=False)
    worker=Worker(cfg.database_path,None,None,10)
    job={"id":"regenerate-thumb-job","payload_json":json.dumps({"published_video_id":ids["published_id"]})}
    with sqlite3.connect(ids["db"]) as db:
        db.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (job["id"],"GENERATE_YOUTUBE_THUMBNAIL","RUNNING",ids["source_id"],ids["media_id"],job["payload_json"],datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc).isoformat()))
    worker.generate_youtube_thumbnail(job)
    with sqlite3.connect(ids["db"]) as db:
        db.row_factory=sqlite3.Row
        pub=db.execute("SELECT custom_thumbnail_path,custom_thumbnail_sha256,custom_thumbnail_width,custom_thumbnail_height,custom_thumbnail_status FROM published_videos WHERE id=?",(ids["published_id"],)).fetchone()
        queued=db.execute("SELECT id,job_type,status,payload_json FROM jobs WHERE job_type='SET_YOUTUBE_THUMBNAIL' AND status='QUEUED'").fetchall()
        gen_status=db.execute("SELECT status FROM jobs WHERE id=?",(job["id"],)).fetchone()[0]
    cover=Path(pub["custom_thumbnail_path"])
    assert cover.is_file()
    assert hashlib.sha256(cover.read_bytes()).hexdigest()==pub["custom_thumbnail_sha256"]
    assert (pub["custom_thumbnail_width"],pub["custom_thumbnail_height"]) in {(1080,1920),(2160,3840)}
    assert pub["custom_thumbnail_status"]=="SET_PENDING"
    assert len(queued)==1 and queued[0]["status"]=="QUEUED"
    assert queued[0]["job_type"]=="SET_YOUTUBE_THUMBNAIL"
    assert json.loads(queued[0]["payload_json"])["published_video_id"]==ids["published_id"]
    assert gen_status=="COMPLETED"
    # The generator writes cover artifacts only; it must not change the publication status.
    with sqlite3.connect(ids["db"]) as db:
        assert db.execute("SELECT publishing_status FROM published_videos WHERE id=?",(ids["published_id"],)).fetchone()[0]=="READY_PRIVATE"
    client.__exit__(None,None,None)


def test_set_thumbnail_worker_upload_success_marks_only_thumbnail_status(tmp_path,monkeypatch):
    client,cfg,ids=fixture(tmp_path,monkeypatch,thumbnail_status="SET_PENDING",has_thumbnail=True)
    calls={}
    def fake_set(video_id,path):
        calls["video_id"]=video_id; calls["path"]=str(path)
        return {"video_id":video_id,"uploaded":True,"items":[]}
    monkeypatch.setattr(worker_module,"set_custom_thumbnail",fake_set)
    job={"id":"set-thumb-job","payload_json":json.dumps({"published_video_id":ids["published_id"]})}
    with sqlite3.connect(ids["db"]) as db:
        db.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (job["id"],"SET_YOUTUBE_THUMBNAIL","RUNNING",ids["source_id"],ids["media_id"],job["payload_json"],datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc).isoformat()))
    worker=Worker(cfg.database_path,None,None,10)
    worker.set_youtube_thumbnail(job)
    with sqlite3.connect(ids["db"]) as db:
        db.row_factory=sqlite3.Row
        pub=db.execute("SELECT custom_thumbnail_status,custom_thumbnail_error,custom_thumbnail_uploaded_at,publishing_status FROM published_videos WHERE id=?",(ids["published_id"],)).fetchone()
        task=db.execute("SELECT status,payload_json FROM jobs WHERE id=?",(job["id"],)).fetchone()
    assert calls["video_id"]=="youtube-video-123"
    assert Path(calls["path"]).is_file()
    assert pub["custom_thumbnail_status"]=="SET_SUCCESS"
    assert pub["custom_thumbnail_error"]==""
    assert pub["custom_thumbnail_uploaded_at"]
    assert pub["publishing_status"]=="READY_PRIVATE"
    assert task["status"]=="COMPLETED"


def test_thumbnail_failure_does_not_change_video_publication_status(tmp_path,monkeypatch):
    from app.integrations.youtube import ThumbnailUploadError
    client,cfg,ids=fixture(tmp_path,monkeypatch,thumbnail_status="SET_PENDING",has_thumbnail=True)
    def fake_set(*args): raise ThumbnailUploadError("custom thumbnail not eligible",retryable=False)
    monkeypatch.setattr(worker_module,"set_custom_thumbnail",fake_set)
    worker=Worker(cfg.database_path,None,None,10)
    job_id="set-thumb-failed-job"
    stamp=datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(ids["db"]) as db:
        db.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (job_id,"SET_YOUTUBE_THUMBNAIL","QUEUED",ids["source_id"],ids["media_id"],json.dumps({"published_video_id":ids["published_id"]}),stamp,stamp))
    assert worker.one() is True
    with sqlite3.connect(ids["db"]) as db:
        db.row_factory=sqlite3.Row
        pub=db.execute("SELECT custom_thumbnail_status,custom_thumbnail_error,publishing_status FROM published_videos WHERE id=?",(ids["published_id"],)).fetchone()
        task=db.execute("SELECT status,error FROM jobs WHERE id=?",(job_id,)).fetchone()
    assert pub["custom_thumbnail_status"]=="FAILED"
    assert "not eligible" in pub["custom_thumbnail_error"]
    assert pub["publishing_status"]=="READY_PRIVATE"
    assert task["status"]=="FAILED"
    assert "not eligible" in task["error"]
    client.__exit__(None,None,None)


def test_retry_thumbnail_endpoint_regenerates_missing_cover_and_respects_gates(tmp_path,monkeypatch):
    client,cfg,ids=fixture(tmp_path,monkeypatch,thumbnail_status="GENERATION_FAILED",has_thumbnail=False)
    import app.main as main_module
    monkeypatch.setattr(main_module,"connected_status",lambda:{"connected":True,"channel_title":"Synthetic CI"})
    response=client.post(f"/api/publishing/videos/{ids['published_id']}/retry-thumbnail")
    assert response.status_code==202,response.text
    assert response.json()["job_type"]=="GENERATE_YOUTUBE_THUMBNAIL"
    with sqlite3.connect(ids["db"]) as db:
        row=db.execute("SELECT job_type,status FROM jobs WHERE id=?",(response.json()["id"],)).fetchone()
    assert tuple(row)==("GENERATE_YOUTUBE_THUMBNAIL","QUEUED")
    client.__exit__(None,None,None)


def test_custom_thumbnail_preview_checks_hash_and_returns_jpeg(tmp_path,monkeypatch):
    client,cfg,ids=fixture(tmp_path,monkeypatch,thumbnail_status="SET_PENDING",has_thumbnail=True)
    response=client.get(f"/api/publishing/videos/{ids['published_id']}/thumbnail")
    assert response.status_code==200,response.text[:300]
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.content[:2]==bytes((255,216))
    corrupt_path=Path(response.headers.get("x-no-such-header",""))
    with sqlite3.connect(ids["db"]) as db:
        pub=db.execute("SELECT custom_thumbnail_path FROM published_videos WHERE id=?",(ids["published_id"],)).fetchone()[0]
        with open(pub,"ab") as handle: handle.write(b"tampered")
    corrupt=client.get(f"/api/publishing/videos/{ids['published_id']}/thumbnail")
    assert corrupt.status_code==409
    client.__exit__(None,None,None)


def test_retry_thumbnail_is_blocked_by_global_pause(tmp_path,monkeypatch):
    import app.main as main_module
    client,cfg,ids=fixture(tmp_path,monkeypatch,thumbnail_status="SET_PENDING",has_thumbnail=True)
    monkeypatch.setattr(main_module,"connected_status",lambda:{"connected":True,"channel_title":"Synthetic CI"})
    assert client.post("/api/system/pause",json={"paused":True}).status_code==200
    response=client.post(f"/api/publishing/videos/{ids['published_id']}/retry-thumbnail")
    assert response.status_code==423,response.text
    with sqlite3.connect(ids["db"]) as db:
        count=db.execute("SELECT COUNT(*) FROM jobs WHERE job_type IN ('GENERATE_YOUTUBE_THUMBNAIL','SET_YOUTUBE_THUMBNAIL')").fetchone()[0]
    assert count==0
    client.__exit__(None,None,None)


def test_retry_thumbnail_upload_when_artifact_is_valid(tmp_path,monkeypatch):
    import app.main as main_module
    client,cfg,ids=fixture(tmp_path,monkeypatch,thumbnail_status="FAILED",has_thumbnail=True)
    monkeypatch.setattr(main_module,"connected_status",lambda:{"connected":True,"channel_title":"Synthetic CI"})
    response=client.post(f"/api/publishing/videos/{ids['published_id']}/retry-thumbnail")
    assert response.status_code==202,response.text
    assert response.json()["job_type"]=="SET_YOUTUBE_THUMBNAIL"
    with sqlite3.connect(ids["db"]) as db:
        job=db.execute("SELECT job_type,status FROM jobs WHERE id=?",(response.json()["id"],)).fetchone()
        status=db.execute("SELECT custom_thumbnail_status FROM published_videos WHERE id=?",(ids["published_id"],)).fetchone()[0]
    assert tuple(job)==("SET_YOUTUBE_THUMBNAIL","QUEUED")
    assert status=="SET_PENDING"
    client.__exit__(None,None,None)


def test_processing_success_queues_generated_thumbnail_after_remote_processing(tmp_path,monkeypatch):
    client,cfg,ids=fixture(tmp_path,monkeypatch,thumbnail_status="GENERATED",has_thumbnail=True)
    import json
    import app.worker as worker_mod
    class Req:
        def execute(self):
            return {"items":[{"id":"youtube-video-123","processingDetails":{"processingStatus":"succeeded"},"status":{"privacyStatus":"private"}}]}
    class Videos:
        def list(self,**kwargs): return Req()
    class Service:
        def videos(self): return Videos()
    monkeypatch.setattr(worker_mod,"youtube_service",lambda:Service())
    stamp=datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(ids["db"]) as db:
        db.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("check-thumb-processing","CHECK_YOUTUBE_PROCESSING","RUNNING",ids["source_id"],ids["media_id"],
             json.dumps({"published_video_id":ids["published_id"],"poll_attempt":0,"max_polls":5}),stamp,stamp))
        db.execute("UPDATE published_videos SET publishing_status='PROCESSING',remote_processing_status='processing' WHERE id=?",(ids["published_id"],))
    worker=Worker(cfg.database_path,None,None,10)
    worker.check_youtube_processing({"id":"check-thumb-processing","payload_json":json.dumps({"published_video_id":ids["published_id"],"poll_attempt":0,"max_polls":5})})
    with sqlite3.connect(ids["db"]) as db:
        db.row_factory=sqlite3.Row
        jobs=db.execute("SELECT job_type,status,payload_json FROM jobs WHERE job_type='SET_YOUTUBE_THUMBNAIL'").fetchall()
        pub=db.execute("SELECT publishing_status,custom_thumbnail_status,remote_processing_status FROM published_videos WHERE id=?",(ids["published_id"],)).fetchone()
    assert len(jobs)==1
    assert jobs[0]["status"]=="QUEUED"
    assert json.loads(jobs[0]["payload_json"])["published_video_id"]==ids["published_id"]
    assert tuple(pub)==("READY_PRIVATE","SET_PENDING","succeeded")
    client.__exit__(None,None,None)
