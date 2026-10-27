from pathlib import Path
from datetime import datetime, timezone
import shutil
import sqlite3
import subprocess
import sys
import json
import hashlib
import math
import struct
import wave
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
        assert render["thumbnail_width"]==720 and render["thumbnail_height"]==1280
        thumbnail_path=Path(render["thumbnail_path"])
        assert thumbnail_path.is_file() and thumbnail_path.stat().st_size>0
        poster_response=client.get(f"/api/renders/{render['id']}/thumbnail")
        assert poster_response.status_code==200,poster_response.text[:300]
        assert poster_response.headers["content-type"].startswith("image/jpeg")
        assert poster_response.content[:2]==bytes((255,216))
        qc_report=render["qc_report"]
        assert qc_report["captions_burned"] is False
        sidecar=Path(qc_report["caption_sidecar_path"])
        assert sidecar.is_file()
        subtitle=sidecar.read_text(encoding="utf-8")
        assert "1\n00:00:00,500 --> 00:00:01,500\nNice shot!\n" in subtitle
        candidates=client.get("/api/candidates").json()
        assert next(x for x in candidates if x["id"]==cid)["state"]=="RENDERED_QC_PASSED"

        # Voiceover sidecar can be mixed into gameplay audio with the selected render profile.
        voice_cid="ci-candidate-narration"
        voice_jid="ci-render-job-narration"
        voice_path=data/"outputs"/"ci-narration.wav"
        voice_path.parent.mkdir(parents=True,exist_ok=True)
        sample_rate=22050
        with wave.open(str(voice_path),"wb") as voice:
            voice.setnchannels(1)
            voice.setsampwidth(2)
            voice.setframerate(sample_rate)
            frames=[]
            for i in range(int(sample_rate*1.5)):
                sample=int(5000*math.sin(2*math.pi*220*i/sample_rate))
                frames.append(struct.pack("<h",sample))
            voice.writeframes(b"".join(frames))
        voice_sha=hashlib.sha256(voice_path.read_bytes()).hexdigest()
        with sqlite3.connect(cfg.database_path) as db:
            db.execute("INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       (voice_cid,mid,1.0,5.0,"test_fixture","Narration mixer CI candidate","APPROVED",stamp))
            db.execute("""INSERT INTO content_plans(id,candidate_id,chosen_format,title,narration_text,created_at,updated_at,
                narration_audio_path,narration_audio_sha256,narration_audio_duration_seconds,narration_audio_status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("ci-narration-plan",voice_cid,"gameplay_highlight","Narration test clip","Watch the rotate.",stamp,stamp,
                 str(voice_path),voice_sha,1.5,"READY"))
            db.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       (voice_jid,"RENDER_CANDIDATE","QUEUED",sid,mid,json.dumps({"candidate_id":voice_cid,"render_profile":"fit_pad"}),stamp,stamp))
        assert worker.one() is True
        voice_job=next(x for x in client.get("/api/jobs").json() if x["id"]==voice_jid)
        assert voice_job["status"]=="COMPLETED",voice_job.get("error")
        voice_render=next(x for x in client.get("/api/renders").json() if x["candidate_id"]==voice_cid)
        assert voice_render["qc_status"]=="PASS",voice_render["qc_report"]
        assert voice_render["audio_codec"]=="aac"
        assert voice_render["qc_report"]["narration_mixed"] is True
        assert voice_render["qc_report"]["narration_audio_sha256"]==voice_sha
        assert 1.0<=voice_render["qc_report"]["narration_audio_duration_seconds"]<=2.0
        assert Path(voice_render["output_path"]).is_file()

        blur_cid="ci-candidate-blur-fill"
        blur_jid="ci-render-job-blur-fill"
        with sqlite3.connect(cfg.database_path) as db:
            db.execute("INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       (blur_cid,mid,2.0,6.0,"test_fixture","Blurred-backdrop CI candidate","APPROVED",stamp))
            db.execute("INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       (blur_jid,"RENDER_CANDIDATE","QUEUED",sid,mid,json.dumps({"candidate_id":blur_cid,"render_profile":"blur_fill"}),stamp,stamp))
        assert worker.one() is True
        blur_job=next(x for x in client.get("/api/jobs").json() if x["id"]==blur_jid)
        assert blur_job["status"]=="COMPLETED",blur_job.get("error")
        blur_render=next(x for x in client.get("/api/renders").json() if x["candidate_id"]==blur_cid)
        assert blur_render["qc_status"]=="PASS",blur_render["qc_report"]
        assert blur_render["render_profile"]=="blur_fill"
        assert blur_render["thumbnail_width"]==720 and blur_render["thumbnail_height"]==1280
        assert Path(blur_render["thumbnail_path"]).is_file()
        assert blur_render["width"]==1080 and blur_render["height"]==1920
        assert Path(blur_render["output_path"]).is_file()
        assert blur_render["qc_report"]["layout"]=="blur_fill"
        assert "full source frame preserved" in blur_render["qc_report"]["profile_notes"][0]
