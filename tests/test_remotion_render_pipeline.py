from pathlib import Path
from datetime import datetime, timezone
import json
import shutil
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings
from app.main import create_app
from app.worker import Worker


def test_authorized_candidate_renders_through_remotion_without_poster_or_voiceover(tmp_path, monkeypatch):
    """Exercise the real SQLite/rights/worker/QC path using only generated synthetic media."""
    node = shutil.which("node")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    renderer_package = ROOT / "remotion" / "node_modules" / "@remotion" / "renderer" / "package.json"
    bundler_package = ROOT / "remotion" / "node_modules" / "@remotion" / "bundler" / "package.json"
    if not node or not ffmpeg or not ffprobe:
        pytest.skip("Node.js, FFmpeg and FFprobe are required for the Remotion worker integration test")
    if not renderer_package.is_file() or not bundler_package.is_file():
        pytest.skip("Run scripts/setup-remotion.bat or npm install in remotion/ before this test")

    monkeypatch.setenv("GCF_RENDER_ENGINE", "remotion")
    monkeypatch.setenv("GCF_PROJECT_ROOT", str(ROOT))
    monkeypatch.setenv("GCF_BURN_CAPTIONS", "false")
    monkeypatch.setenv("GCF_ENABLE_YOUTUBE_UPLOADS", "false")
    monkeypatch.setenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING", "false")
    monkeypatch.setenv("GCF_REMOTION_TIMEOUT_SECONDS", "900")

    root = tmp_path / "gcf-remotion-test"
    data = root / "data"
    media = data / "media"
    cfg = Settings(root, data, media, data / "factory.sqlite3", ffmpeg, ffprobe, 90)

    with TestClient(create_app(settings=cfg, start_worker=False)) as client:
        source_response = client.post("/api/sources", json={
            "source_url": "https://example.com/synthetic-remotion-test",
            "platform": "test",
            "title": "Synthetic Remotion acceptance footage",
            "creator": "Local CI fixture",
        })
        assert source_response.status_code == 201, source_response.text
        source_id = source_response.json()["id"]

        rights_response = client.put(f"/api/sources/{source_id}/rights", json={
            "status": "AUTHORIZED",
            "license_type": "CI-generated synthetic media",
            "permission_evidence": "Created by FFmpeg within the isolated test run",
            "editing_allowed": True,
            "commercial_use_allowed": True,
            "reviewed_by_owner": True,
        })
        assert rights_response.status_code == 200, rights_response.text

        input_path = media / "synthetic.mp4"
        subprocess.run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=8",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=8",
            "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-y", str(input_path),
        ], capture_output=True, text=True, timeout=60, check=True)

        import_response = client.post("/api/media/import", json={
            "source_id": source_id,
            "path": "synthetic.mp4",
        })
        assert import_response.status_code == 201, import_response.text
        media_id = import_response.json()["id"]

        candidate_id = "remotion-ci-candidate"
        job_id = "remotion-ci-render-job"
        stamp = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(cfg.database_path) as db:
            db.execute(
                """INSERT INTO candidate_moments(
                    id,media_id,start_seconds,end_seconds,detector,explanation,state,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (candidate_id, media_id, 1.0, 5.0, "remotion_ci",
                 "Synthetic approved candidate for Remotion render acceptance", "APPROVED", stamp),
            )
            db.execute(
                """INSERT INTO content_plans(
                    id,candidate_id,chosen_format,title,narration_text,created_at,updated_at,
                    narration_audio_status
                ) VALUES(?,?,?,?,?,?,?,?)""",
                ("remotion-ci-plan", candidate_id, "gameplay_highlight", "Synthetic clip",
                 "", stamp, stamp, "SYNTHESIZING"),
            )
            db.execute(
                """INSERT INTO caption_segments(
                    id,candidate_id,segment_index,start_seconds,end_seconds,text,confidence,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                ("remotion-ci-caption", candidate_id, 0, 1.5, 2.5,
                 "Synthetic highlight!", 1.0, stamp),
            )
            db.execute(
                """INSERT INTO jobs(
                    id,job_type,status,source_id,media_id,payload_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (job_id, "RENDER_CANDIDATE", "QUEUED", source_id, media_id,
                 json.dumps({"candidate_id": candidate_id, "render_profile": "fit_pad"}),
                 stamp, stamp),
            )

        worker = Worker(cfg.database_path, ffmpeg, ffprobe, 90, media_root=media)
        assert worker.one() is True

        jobs = client.get("/api/jobs").json()
        job = next(row for row in jobs if row["id"] == job_id)
        assert job["status"] == "COMPLETED", job.get("error")

        renders = client.get("/api/renders").json()
        render = next(row for row in renders if row["candidate_id"] == candidate_id)
        assert render["qc_status"] == "PASS", render.get("qc_report")
        assert render["render_profile"] == "fit_pad"
        assert render["width"] == 1080 and render["height"] == 1920
        assert render["video_codec"] == "h264"
        assert render["audio_codec"] == "aac"
        assert render["file_size"] > 1000
        output_path = Path(render["output_path"])
        assert output_path.is_file() and output_path.stat().st_size == render["file_size"]
        assert render["thumbnail_path"] is None
        assert render["thumbnail_width"] is None and render["thumbnail_height"] is None
        assert not output_path.with_suffix(".thumbnail.jpg").exists()

        report = render["qc_report"]
        assert report["render_engine"] == "remotion"
        assert report["remotion_encoder_settings"] == {
            "codec": "h264",
            "crf": 16,
            "x264Preset": "slow",
            "audioCodec": "aac",
            "audioBitrate": "192K",
        }
        assert report["captions_burned"] is True
        assert report["checks"]["narration_mixed"] is False
        assert report["checks"]["ffmpeg_decode_scan"] == "PASS"

        # Independently verify playable duration and stream metadata, rather than trusting DB only.
        probed = subprocess.run([
            ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output_path),
        ], capture_output=True, text=True, timeout=30, check=True)
        details = json.loads(probed.stdout)
        video = next(stream for stream in details["streams"] if stream["codec_type"] == "video")
        audio = next(stream for stream in details["streams"] if stream["codec_type"] == "audio")
        assert (video["width"], video["height"], video["codec_name"]) == (1080, 1920, "h264")
        assert audio["codec_name"] == "aac"
        assert 3.5 <= float(details["format"]["duration"]) <= 4.5

        assert worker._cleanup_unregistered_render_artifacts(candidate_id) == []
        assert output_path.is_file(), "Cleanup must preserve the successful registered Remotion render."
