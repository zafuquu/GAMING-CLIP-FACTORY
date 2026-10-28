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
        assert worker._cleanup_unregistered_render_artifacts(cid)==[]
        assert Path(render["output_path"]).is_file(), "Cleanup must preserve the registered successful render."
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
        assert isinstance(title, str) and title.strip() and len(title) <= 100
        assert isinstance(description, str) and len(description) <= 5000
        assert isinstance(tags, list) and len(tags) <= 30
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

        # Step 2 — generate six full-frame colors with hard scene cuts and a moving white box.
        # This is synthetic test material only; no third-party content is involved.
        incoming = media / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        input_path = incoming / "mock_gameplay.mp4"
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i",
            "color=c=red:s=320x180:r=24:d=12,drawbox=x='mod(t*60,iw-40)':y=70:w=40:h=40:color=white:t=fill",
            "-f", "lavfi", "-i",
            "color=c=lime:s=320x180:r=24:d=12,drawbox=x='mod(t*60,iw-40)':y=70:w=40:h=40:color=white:t=fill",
            "-f", "lavfi", "-i",
            "color=c=blue:s=320x180:r=24:d=12,drawbox=x='mod(t*60,iw-40)':y=70:w=40:h=40:color=white:t=fill",
            "-f", "lavfi", "-i",
            "color=c=yellow:s=320x180:r=24:d=12,drawbox=x='mod(t*60,iw-40)':y=70:w=40:h=40:color=white:t=fill",
            "-f", "lavfi", "-i",
            "color=c=magenta:s=320x180:r=24:d=12,drawbox=x='mod(t*60,iw-40)':y=70:w=40:h=40:color=white:t=fill",
            "-f", "lavfi", "-i",
            "color=c=cyan:s=320x180:r=24:d=12,drawbox=x='mod(t*60,iw-40)':y=70:w=40:h=40:color=white:t=fill",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=72",
            "-filter_complex", "[0:v][1:v][2:v][3:v][4:v][5:v]concat=n=6:v=1:a=0[v]",
            "-map", "[v]", "-map", "6:a", "-t", "72",
            "-af", "volume=eval=frame:volume='if(between(t\\,20\\,22)\\,18\\,0.1)'",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k", str(input_path),
        ]
        subprocess.run(command, capture_output=True, text=True, timeout=90, check=True)

        # Step 3 — explicitly enable the credential-gated upload setting in this isolated test.
        # Only the upload provider network call is mocked; local rights/QC/hash gates stay real.
        enable_response = client.post("/api/publishing/enable", json={"enabled": True})
        assert enable_response.status_code == 200, enable_response.text

        # Opt in to the watched-folder pipeline including private upload after QC PASS.
        # Public release remains disabled and is never queued by this feature.
        watch_response = client.post(
            "/api/automation/folder-watches",
            json={
                "source_id": source_id,
                "relative_path": "incoming",
                "enabled": True,
                "recursive": False,
                "auto_analyze": True,
                "auto_render": True,
                "auto_upload_private": True,
                "scan_interval_seconds": 15,
            },
        )
        assert watch_response.status_code == 201, watch_response.text
        watch_id = watch_response.json()["id"]
        assert watch_response.json()["auto_render"] is True
        assert watch_response.json()["auto_upload_private"] is True
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

        # Step 4 — signal-only candidates must never be auto-rendered. This fixture
        # includes a deliberate audio-energy event, but without a completed semantic
        # vision assessment it must stay in NEEDS_REVIEW.
        assert worker.one() is True
        analysis_job = next(job for job in client.get("/api/jobs").json() if job["id"] == analysis_row[0])
        assert analysis_job["status"] == "COMPLETED", analysis_job.get("error")
        analysis_summary = json.loads(analysis_job["payload_json"])
        assert analysis_summary["auto_render_queued"] == 0, analysis_summary
        assert analysis_summary["ranking_method"] == "local_audio_motion_scene_v2", analysis_summary
        assert analysis_summary["video_motion_sample_count"] >= 3, analysis_summary
        assert analysis_summary["ranking_calibrated"] is False

        candidates = [
            candidate for candidate in client.get("/api/candidates").json()
            if candidate["media_id"] == media_id
        ]
        assert candidates, "The deliberate audio-energy event should produce a review candidate."
        assert all(candidate["detector"] == "local_audio_motion_scene_ranker_v2" for candidate in candidates)
        assert all(candidate["ranking_score"] is not None for candidate in candidates)
        assert all(candidate["state"] == "NEEDS_REVIEW" for candidate in candidates)
        assert not [
            job for job in client.get("/api/jobs").json()
            if job["media_id"] == media_id and job["job_type"] == "RENDER_CANDIDATE"
            and job["status"] == "QUEUED"
        ], "No candidate may auto-render without passing semantic vision review."

        # Seed a completed, strong, evidence-grounded assessment to exercise the
        # actual worker's post-assessment auto-render gate without requiring Ollama in CI.
        selected = candidates[0]
        stamp = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(cfg.database_path) as db:
            db.execute(
                """INSERT INTO candidate_vision_assessments(
                    id,candidate_id,model_name,status,gameplay_context,event_type,visible_event,
                    is_actionable_highlight,highlight_score,confidence,evidence_json,
                    quality_issues_json,frame_times_json,raw_response_json,error,created_at,updated_at
                ) VALUES(?,?,?,'COMPLETED',?,?,?,?,?,?,?,?,?,?, '',?,?)""",
                (
                    "vision-gate-ci-assessment", selected["id"], "ci-mocked-local-vision",
                    "Visible gameplay elimination", "elimination", "Visible elimination indicator",
                    1, 0.91, 0.89, json.dumps(["Visible elimination indicator in HUD"]),
                    "[]", "[]", "{}", stamp, stamp,
                ),
            )
            db.execute(
                """INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at)
                   VALUES(?,'ANALYZE_MEDIA','QUEUED',?,?, ?,?,?)""",
                (
                    "vision-qualified-auto-render", source_id, media_id,
                    json.dumps({"folder_watch_id": watch_id}), stamp, stamp,
                ),
            )
        assert worker.one() is True
        qualified_job = next(
            job for job in client.get("/api/jobs").json() if job["id"] == "vision-qualified-auto-render"
        )
        assert qualified_job["status"] == "COMPLETED", qualified_job.get("error")
        qualified_summary = json.loads(qualified_job["payload_json"])
        assert qualified_summary["auto_render_queued"] == 1, qualified_summary

        render_jobs = [
            job for job in client.get("/api/jobs").json()
            if job["media_id"] == media_id and job["job_type"] == "RENDER_CANDIDATE"
            and job["status"] == "QUEUED"
        ]
        assert len(render_jobs) == 1
        render_job_id = render_jobs[0]["id"]
        assert worker.one() is True
        final_jobs = {job["id"]: job for job in client.get("/api/jobs").json()}
        assert final_jobs[render_job_id]["status"] == "COMPLETED", final_jobs[render_job_id].get("error")

        selected_after_render = next(
            candidate for candidate in client.get("/api/candidates").json()
            if candidate["id"] == selected["id"]
        )
        assert selected_after_render["state"] == "RENDERED_QC_PASSED", selected_after_render
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

        # Re-analysis is idempotent and cannot exceed the lifetime auto-render cap.
        repeat_analysis_id = "repeat-auto-render-cap"
        repeat_stamp = "2000-01-01T00:00:00+00:00"
        with sqlite3.connect(cfg.database_path) as db:
            db.execute(
                """INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at)
                   VALUES(?,'ANALYZE_MEDIA','QUEUED',?,?, ?,?,?)""",
                (
                    repeat_analysis_id, source_id, media_id,
                    json.dumps({"folder_watch_id": watch_id}), repeat_stamp, repeat_stamp,
                ),
            )
        assert worker.one() is True
        repeat_job = next(job for job in client.get("/api/jobs").json() if job["id"] == repeat_analysis_id)
        assert repeat_job["status"] == "COMPLETED", repeat_job.get("error")
        repeat_summary = json.loads(repeat_job["payload_json"])
        assert repeat_summary["auto_render_queued"] == 0, repeat_summary
        assert repeat_summary["auto_rendered_historically"] == 1, repeat_summary
        with sqlite3.connect(cfg.database_path) as db:
            auto_rendered_count = db.execute(
                "SELECT COUNT(*) FROM candidate_moments WHERE media_id=? AND auto_rendered=1",
                (media_id,),
            ).fetchone()[0]
        assert auto_rendered_count == 1, "Persistent candidate markers must enforce the lifetime per-media cap."
        # Step 5 — the real render completion path should enqueue exactly one private upload
        # for this media after a QC-PASS output and all live rights/settings checks.
        # The per-media cap prevents one long recording from flooding the channel.
        with sqlite3.connect(cfg.database_path) as db:
            auto_publication = db.execute(
                "SELECT id,rendered_output_id,title FROM published_videos WHERE media_id=?",
                (media_id,),
            ).fetchone()
            upload_jobs = db.execute(
                "SELECT payload_json FROM jobs WHERE job_type='UPLOAD_YOUTUBE' AND status IN ('QUEUED','RUNNING')"
            ).fetchall()
            public_jobs_before_upload = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE job_type='PUBLISH_YOUTUBE' AND status IN ('QUEUED','RUNNING')"
            ).fetchone()[0]
        assert auto_publication is not None, "QC-passed watched-folder render must queue a private upload when explicitly enabled."
        assert auto_publication[2].strip(), "Automatic upload must have a nonempty title."
        assert len(upload_jobs) == 1, "Only one automatic private upload may be queued per media file."
        assert json.loads(upload_jobs[0][0])["publication_mode"] == "private"
        assert json.loads(upload_jobs[0][0])["publish_at"] is None
        assert public_jobs_before_upload == 0, "Automatic private upload must never queue public release."
        assert worker.one() is True
        assert len(upload_calls) == 1

        publication_id = auto_publication[0]
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


def test_failed_render_qc_removes_unregistered_output_and_preserves_source(tmp_path, monkeypatch):
    """If FFmpeg succeeds but the output probe/QC fails, orphan media must be removed."""
    import app.worker as worker_module

    root=tmp_path/"cleanup-project"
    data=root/"data"
    media=data/"media"
    media.mkdir(parents=True,exist_ok=True)
    cfg=Settings(root,data,media,data/"factory.sqlite3","fake-ffmpeg","fake-ffprobe",30)
    with TestClient(create_app(settings=cfg,start_worker=False)) as client:
        source=client.post("/api/sources",json={
            "source_url":"https://example.com/synthetic-cleanup-fixture",
            "platform":"test",
            "title":"Synthetic cleanup fixture",
        })
        assert source.status_code==201,source.text
        source_id=source.json()["id"]
        rights=client.put(f"/api/sources/{source_id}/rights",json={
            "status":"AUTHORIZED",
            "license_type":"Synthetic CI fixture",
            "permission_evidence":"No third-party media; bytes are a local fake input for mocked FFprobe/FFmpeg.",
            "editing_allowed":True,
            "commercial_use_allowed":True,
            "reviewed_by_owner":True,
        })
        assert rights.status_code==200,rights.text

        source_path=media/"fake_source.mp4"
        source_bytes=b"synthetic source fixture"
        source_path.write_bytes(source_bytes)
        imported=client.post("/api/media/import",json={"source_id":source_id,"path":source_path.name})
        assert imported.status_code==201,imported.text
        media_id=imported.json()["id"]
        candidate_id="cleanup-candidate"
        job_id="cleanup-render-job"
        stamp=datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(cfg.database_path) as db:
            db.execute(
                "INSERT INTO candidate_moments(id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (candidate_id,media_id,1.0,5.0,"test_fixture","QC failure cleanup fixture","APPROVED",stamp),
            )
            db.execute(
                "INSERT INTO jobs(id,job_type,status,source_id,media_id,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (job_id,"RENDER_CANDIDATE","QUEUED",source_id,media_id,json.dumps({"candidate_id":candidate_id}),stamp,stamp),
            )
            # Make this injected failure terminal on the first attempt so the test can
            # assert both job/candidate failure state as well as artifact cleanup.
            db.execute("UPDATE jobs SET max_attempts=1 WHERE id=?",(job_id,))

        def fake_media_command(args, timeout, cwd=None):
            executable=Path(args[0]).name.lower()
            target=Path(args[-1]).resolve()
            if executable=="fake-ffprobe":
                if target==source_path.resolve():
                    return subprocess.CompletedProcess(args,0,stdout=json.dumps({
                        "format":{"format_name":"mp4","duration":"8","size":str(len(source_bytes))},
                        "streams":[
                            {"codec_type":"video","codec_name":"h264","width":320,"height":180,"avg_frame_rate":"30/1"},
                            {"codec_type":"audio","codec_name":"aac"},
                        ],
                    }),stderr="")
                # FFmpeg created an artifact, but it lacks the expected video stream/dimensions.
                return subprocess.CompletedProcess(args,0,stdout=json.dumps({
                    "format":{"duration":"4"},
                    "streams":[{"codec_type":"audio","codec_name":"aac"}],
                }),stderr="")
            if executable=="fake-ffmpeg" and "-c:v" in args:
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(b"partial output created before mocked QC failure")
                return subprocess.CompletedProcess(args,0,stdout="",stderr="")
            raise AssertionError(f"Unexpected mocked media command: {args}")

        monkeypatch.setattr(worker_module,"run",fake_media_command)
        worker=Worker(cfg.database_path,"fake-ffmpeg","fake-ffprobe",30,media_root=media)
        assert worker.one() is True
        failed_job=next(j for j in client.get("/api/jobs").json() if j["id"]==job_id)
        assert failed_job["status"]=="FAILED",failed_job
        assert "missing expected 1080x1920 video" in failed_job["error"]
        failed_candidate=next(c for c in client.get("/api/candidates").json() if c["id"]==candidate_id)
        assert failed_candidate["state"]=="RENDER_FAILED"
        outputs=data/"outputs"
        leftovers=[p.name for p in outputs.iterdir() if p.is_file() and p.name.startswith(candidate_id+"_")]
        assert leftovers==[],f"Failed render left unregistered files behind: {leftovers}"
        assert source_path.read_bytes()==source_bytes,"Cleanup must never remove or mutate the original source media."
