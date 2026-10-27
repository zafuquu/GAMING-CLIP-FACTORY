from pathlib import Path
from datetime import datetime, timedelta, timezone
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
                       (voice_cid,mid,1.0,5.0,"test_fixture_narration","Narration mixer CI candidate","APPROVED",stamp))
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
        assert voice_render["qc_report"]["checks"]["narration_mixed"] is True
        assert voice_render["qc_report"]["checks"]["narration_audio_sha256"]==voice_sha
        assert 1.0<=voice_render["qc_report"]["checks"]["narration_audio_duration_seconds"]<=2.0
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

def test_mock_production_flow_analysis_review_render_private_upload_and_processing(
    tmp_path, monkeypatch
):
    """Exercise the real local pipeline with synthetic footage and a mocked YouTube API."""
    import app.main as main_module
    import app.worker as worker_module

    monkeypatch.setenv("GCF_BURN_CAPTIONS", "false")
    monkeypatch.setenv("GCF_ENABLE_YOUTUBE_UPLOADS", "true")
    monkeypatch.setenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING", "false")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg/FFprobe are required for the synthetic production-flow test")

    root = tmp_path / "mock-production"
    data = root / "data"
    media = data / "media"
    cfg = Settings(root, data, media, data / "factory.sqlite3", ffmpeg, ffprobe, 90)
    upload_calls = []

    class MockRequest:
        def __init__(self, response):
            self.response = response

        def execute(self):
            return self.response

    class MockVideos:
        def list(self, **kwargs):
            assert kwargs.get("part") == "processingDetails,status"
            assert kwargs.get("id") == "mockVideo123"
            return MockRequest({
                "items": [{
                    "id": "mockVideo123",
                    "processingDetails": {"processingStatus": "succeeded"},
                    "status": {"privacyStatus": "private"},
                }]
            })

    class MockYouTube:
        def videos(self):
            return MockVideos()

    def fake_upload_video(path, title, description, tags):
        assert Path(path).is_file()
        assert title == "CI mock production clip"
        assert description == "Synthetic footage; no third-party content."
        assert tags == ["automation-test"]
        upload_calls.append(Path(path))
        return {
            "video_id": "mockVideo123",
            "url": "https://www.youtube.com/watch?v=mockVideo123",
        }

    monkeypatch.setattr(main_module, "connected_status", lambda: {"connected": True})
    monkeypatch.setattr(worker_module, "upload_video", fake_upload_video)
    monkeypatch.setattr(worker_module, "youtube_service", lambda: MockYouTube())

    with TestClient(create_app(settings=cfg, start_worker=False)) as client:
        # Step 1 — register synthetic footage and require explicit documented authorization.
        source_response = client.post(
            "/api/sources",
            json={
                "source_url": "https://example.com/mock-production-source",
                "platform": "synthetic-ci",
                "title": "Synthetic production acceptance footage",
                "creator": "CI fixture",
                "game": "synthetic-test",
            },
        )
        assert source_response.status_code == 201, source_response.text
        source_id = source_response.json()["id"]
        assert source_response.json()["rights_status"] == "UNKNOWN"

        rights_response = client.put(
            f"/api/sources/{source_id}/rights",
            json={
                "status": "AUTHORIZED",
                "license_type": "Synthetic footage generated for CI",
                "permission_evidence": "Created by FFmpeg within this isolated test; no third-party material",
                "editing_allowed": True,
                "commercial_use_allowed": True,
                "reviewed_by_owner": True,
            },
        )
        assert rights_response.status_code == 200, rights_response.text

        # Step 2 — generate four full-frame colors with hard scene cuts and a moving white box.
        # This is synthetic test material only; no third-party content is involved.
        incoming = media / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        input_path = incoming / "mock_gameplay.mp4"
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i",
            "color=c=red:s=320x180:r=24:d=4,drawbox=x='mod(t*60,iw-40)':y=70:w=40:h=40:color=white:t=fill",
            "-f", "lavfi", "-i",
            "color=c=lime:s=320x180:r=24:d=4,drawbox=x='mod(t*60,iw-40)':y=70:w=40:h=40:color=white:t=fill",
            "-f", "lavfi", "-i",
            "color=c=blue:s=320x180:r=24:d=4,drawbox=x='mod(t*60,iw-40)':y=70:w=40:h=40:color=white:t=fill",
            "-f", "lavfi", "-i",
            "color=c=yellow:s=320x180:r=24:d=4,drawbox=x='mod(t*60,iw-40)':y=70:w=40:h=40:color=white:t=fill",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=16",
            "-filter_complex", "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0[v]",
            "-map", "[v]", "-map", "4:a", "-t", "16",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k", str(input_path),
        ]
        subprocess.run(command, capture_output=True, text=True, timeout=90, check=True)

        # Step 3 — opt in to supervised auto-production for a watched, authorized folder.
        # The folder watcher waits for file stability, imports it, and queues analysis.
        watch_response = client.post(
            "/api/automation/folder-watches",
            json={
                "source_id": source_id,
                "relative_path": "incoming",
                "enabled": True,
                "recursive": False,
                "auto_analyze": True,
                "auto_render": True,
                "scan_interval_seconds": 15,
            },
        )
        assert watch_response.status_code == 201, watch_response.text
        watch_id = watch_response.json()["id"]
        assert watch_response.json()["auto_render"] is True
        worker = Worker(cfg.database_path, ffmpeg, ffprobe, 90, media_root=media)

        first_scan = worker.scan_folder_watch(watch_id)
        assert first_scan["files_seen"] == 1
        assert first_scan["pending_stability"] == 1
        with sqlite3.connect(cfg.database_path) as db:
            old = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
            db.execute(
                "UPDATE watched_media_files SET stable_since=? WHERE watch_id=?",
                (old, watch_id),
            )
        second_scan = worker.scan_folder_watch(watch_id)
        assert second_scan["imported"] == 1, second_scan
        with sqlite3.connect(cfg.database_path) as db:
            media_row = db.execute(
                "SELECT id FROM media_assets WHERE source_id=? ORDER BY created_at DESC LIMIT 1",
                (source_id,),
            ).fetchone()
            assert media_row is not None
            media_id = media_row[0]
            analysis_row = db.execute(
                "SELECT id FROM jobs WHERE media_id=? AND job_type='ANALYZE_MEDIA' ORDER BY created_at DESC LIMIT 1",
                (media_id,),
            ).fetchone()
        assert analysis_row is not None

        # Step 4 — the real analyzer may automatically queue no more than three render jobs
        # for this explicitly configured folder. Every render still runs rights/QC gates.
        assert worker.one() is True
        analysis_job = next(
            job for job in client.get("/api/jobs").json()
            if job["id"] == analysis_row[0]
        )
        assert analysis_job["status"] == "COMPLETED", analysis_job.get("error")
        analysis_summary = json.loads(analysis_job["payload_json"])
        assert analysis_summary["auto_render_queued"] >= 1, analysis_summary
        assert analysis_summary["auto_render_queued"] <= 3, analysis_summary

        candidates = [
            candidate for candidate in client.get("/api/candidates").json()
            if candidate["media_id"] == media_id
        ]
        assert candidates, "Scene-change analysis should create candidates for the generated full-frame cuts."
        assert all(candidate["detector"] == "ffmpeg_scene_change" for candidate in candidates)
        approved = [candidate for candidate in candidates if candidate["state"] == "APPROVED"]
        assert len(approved) == analysis_summary["auto_render_queued"]
        assert len(approved) <= 3
        render_jobs = [
            job for job in client.get("/api/jobs").json()
            if job["media_id"] == media_id
            and job["job_type"] == "RENDER_CANDIDATE"
            and job["status"] == "QUEUED"
        ]
        assert len(render_jobs) == len(approved)
        for _ in render_jobs:
            assert worker.one() is True

        final_jobs = {job["id"]: job for job in client.get("/api/jobs").json()}
        for render_job in render_jobs:
            assert final_jobs[render_job["id"]]["status"] == "COMPLETED", final_jobs[render_job["id"]].get("error")

        selected = approved[0]
        render = next(
            output for output in client.get("/api/renders").json()
            if output["candidate_id"] == selected["id"]
        )
        assert render["qc_status"] == "PASS", render.get("qc_report")
        assert render["width"] == 1080 and render["height"] == 1920
        assert render["video_codec"] == "h264"
        assert render["audio_codec"] == "aac"
        assert Path(render["output_path"]).is_file()
        assert Path(render["thumbnail_path"]).is_file()
        assert render["file_size"] == Path(render["output_path"]).stat().st_size

        # Step 5 — simulate the owner enabling uploads after the normal OAuth setup.
        # Only the provider network operation is mocked; the real rights/QC/hash/API gates run.
        enable_response = client.post("/api/publishing/enable", json={"enabled": True})
        assert enable_response.status_code == 200, enable_response.text
        queue_upload_response = client.post(
            "/api/publishing/schedule",
            json={
                "rendered_output_id": render["id"],
                "title": "CI mock production clip",
                "description": "Synthetic footage; no third-party content.",
                "tags": ["automation-test"],
                "publication_mode": "private",
            },
        )
        assert queue_upload_response.status_code == 202, queue_upload_response.text
        assert queue_upload_response.json()["privacy_status"] == "private"
        assert worker.one() is True
        assert len(upload_calls) == 1

        publication_id = queue_upload_response.json()["published_video_id"]
        publication = next(
            row for row in client.get("/api/publishing/videos").json()
            if row["id"] == publication_id
        )
        assert publication["youtube_video_id"] == "mockVideo123"
        assert publication["publishing_status"] == "PROCESSING"
        assert publication["custom_thumbnail_available"] is True

        # Step 6 — exercise the real processing-state handler against a mocked successful
        # YouTube read. A private upload remains private: no public release job may be queued.
        with sqlite3.connect(cfg.database_path) as db:
            db.execute(
                "UPDATE jobs SET next_attempt_at=NULL WHERE job_type='CHECK_YOUTUBE_PROCESSING' AND status='QUEUED'"
            )
        assert worker.one() is True
        publication = next(
            row for row in client.get("/api/publishing/videos").json()
            if row["id"] == publication_id
        )
        assert publication["remote_processing_status"] == "succeeded"
        assert publication["publishing_status"] == "READY_PRIVATE"
        with sqlite3.connect(cfg.database_path) as db:
            public_jobs = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE job_type='PUBLISH_YOUTUBE' AND status IN ('QUEUED','RUNNING')"
            ).fetchone()[0]
        assert public_jobs == 0
        assert client.get("/api/publishing/status").json()["public_publishing_enabled"] is False
