from pathlib import Path
from datetime import datetime, timezone
import shutil
import sqlite3
import subprocess
import sys
import json
import pytest
from fastapi.testclient import TestClient

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"backend"))
from app.config import Settings
from app.main import create_app
from app.worker import Worker

def test_authorized_clip_renders_and_passes_qc_when_ffmpeg_available(tmp_path,monkeypatch):
    monkeypatch.setenv("GCF_BURN_CAPTIONS","false")
    ffmpeg=shutil.which("ffmpeg")
    ffprobe=shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg/FFprobe are not installed on this runner")
    root=tmp_path/"project"; data=root/"data"; media=data/"media"
    cfg=Settings(root,data,media,data/"factory.sqlite3",ffmpeg,ffprobe,90)
    with TestClient(create_app(settings=cfg,start_worker=False)) as client:
        source=client.post("/api/sources",json={"source_url":"https://example.com/licensed-test-clip","platform":"test","title":"Synthetic authorized test clip"})
        assert source.status_code==201,source.text
        sid=source.json()["id"]
        rights=client.put(f"/api/sources/{sid}/rights",json={"status":"AUTHORIZED","license_type":"Synthetic test footage generated in CI","permission_evidence":"CI-generated synthetic media; not third-party footage","editing_allowed":True,"commercial_use_allowed":True,"reviewed_by_owner":True})
        assert rights.status_code==200,rights.text
        input_path=media/"synthetic.mp4"
        cmd=[ffmpeg,"-hide_banner","-loglevel","error","-nostdin","-f","lavfi","-i","testsrc=size=320x180:rate=24:duration=8","-f","lavfi","-i","sine=frequency=440:duration=8","-shortest","-c:v","libx264","-preset","ultrafast","-pix_fmt","yuv420p","-c:a","aac","-b:a","96k","-y",str(input_path)]
        subprocess.run(cmd,capture_output=True,text=True,timeout=60,check=True)
        imported=client.post("/api/media/import",json={"source_id":sid,"path":"synthetic.mp4"})
        assert imported.status_code==201,imported.text
        mid=imported.json()["id"]
        cid="ci-candidate"
        jid="ci-render-job"
        stamp=datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(cfg.database_path) as db:
            db.execute("INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at) VALUES(?,?,?,?,?,?,?,?)",(cid,mid,1.0,5.0,"test_fixture","Synthetic CI candidate","APPROVED",stamp))
            db.execute("INSERT INTO caption_segments(id,candidate_id,segment_index,start_seconds,end_seconds,text,confidence,created_at) VALUES(?,?,?,?,?,?,?,?)",("ci-caption",cid,0,1.5,2.5,"Nice shot!",0.97,stamp))
            db.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(jid,"RENDER_CANDIDATE","QUEUED",sid,mid,json.dumps({"candidate_id":cid}),stamp,stamp))
        worker=Worker(cfg.database_path,ffmpeg,ffprobe,90)
        assert worker.one() is True
        jobs=client.get("/api/jobs").json()
        job=next(x for x in jobs if x["id"]==jid)
        assert job["status"]=="COMPLETED",job.get("error")
        renders=client.get("/api/renders").json()
        render=next(x for x in renders if x["candidate_id"]==cid)
        assert render["qc_status"]=="PASS"
        assert render["width"]==1080 and render["height"]==1920
        assert Path(render["output_path"]).is_file()
        assert render["file_size"]>0
        assert render["audio_codec"]=="aac"
        qc_report=render["qc_report"]
        assert qc_report["captions_burned"] is False
        sidecar=Path(qc_report["caption_sidecar_path"])
        assert sidecar.is_file()
        subtitle=sidecar.read_text(encoding="utf-8")
        assert "1\\n00:00:00,500 --> 00:00:01,500\\nNice shot!\\n" in subtitle
        candidates=client.get("/api/candidates").json()
        assert next(x for x in candidates if x["id"]==cid)["state"]=="RENDERED_QC_PASSED"
