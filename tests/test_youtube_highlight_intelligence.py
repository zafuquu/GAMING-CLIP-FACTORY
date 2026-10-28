"""End-to-end synthetic acceptance test for YouTube-first local highlight ranking."""
from pathlib import Path
import json
import shutil
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings
from app.main import create_app
from app.worker import Worker


def test_media_analysis_ranks_an_audio_event_into_a_shorts_window(tmp_path, monkeypatch):
    """Exercise actual FFmpeg output parsing and the FastAPI/SQLite worker path."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and FFprobe are required for highlight analysis acceptance.")

    monkeypatch.setenv("GCF_DISK_SAFETY_ENABLED", "false")
    monkeypatch.setenv("GCF_ENABLE_TRANSCRIPTION", "false")
    monkeypatch.setenv("GCF_ENABLE_VISION_ANALYSIS", "false")
    monkeypatch.setenv("GCF_ENABLE_YOUTUBE_UPLOADS", "false")
    monkeypatch.setenv("GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING", "false")

    root = tmp_path / "project"
    data = root / "data"
    media_root = data / "media"
    media_root.mkdir(parents=True)
    cfg = Settings(
        root, data, media_root, data / "factory.sqlite3", ffmpeg, ffprobe, 120
    )

    with TestClient(create_app(settings=cfg, start_worker=False)) as client:
        source = client.post("/api/sources", json={
            "source_url": "https://www.youtube.com/watch?v=TestGameClip01",
            "platform": "youtube",
            "title": "Synthetic gaming footage for local analysis test",
            "creator": "Local CI fixture",
            "game": "Synthetic gameplay",
        })
        assert source.status_code == 201, source.text
        source_id = source.json()["id"]

        rights = client.put(f"/api/sources/{source_id}/rights", json={
            "status": "AUTHORIZED",
            "license_type": "CI-generated synthetic footage",
            "permission_evidence": "Created during this isolated test; no third-party source footage",
            "editing_allowed": True,
            "commercial_use_allowed": True,
            "reviewed_by_owner": True,
        })
        assert rights.status_code == 200, rights.text

        video_path = media_root / "synthetic-gameplay.mp4"
        # The sound level abruptly rises from seconds 20–22. The test checks that
        # the real FFmpeg RMS metadata parser identifies that event and ranks a
        # sensible 15-second Shorts candidate around it without using any network/model.
        subprocess.run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=45",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=45",
            "-filter:a", "volume=eval=frame:volume='if(between(t\\,20\\,22)\\,18\\,0.1)'",
            "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
            "-y", str(video_path),
        ], capture_output=True, text=True, timeout=90, check=True)

        imported = client.post("/api/media/import", json={
            "source_id": source_id,
            "path": "synthetic-gameplay.mp4",
        })
        assert imported.status_code == 201, imported.text
        media_id = imported.json()["id"]

        queued = client.post(f"/api/media/{media_id}/analyze")
        assert queued.status_code == 202, queued.text
        worker = Worker(cfg.database_path, ffmpeg, ffprobe, 120, media_root=media_root)
        assert worker.one() is True

        job_rows = client.get("/api/jobs").json()
        analysis_job = next(item for item in job_rows if item["id"] == queued.json()["id"])
        assert analysis_job["status"] == "COMPLETED", analysis_job.get("error")
        job_payload = json.loads(analysis_job.get("payload_json") or "{}")
        assert job_payload.get("ranking_method") == "local_audio_motion_scene_v2"
        assert job_payload.get("audio_signal_sample_count", 0) >= 40
        assert job_payload.get("video_motion_sample_count", 0) >= 60

        candidates = client.get("/api/candidates?state=NEEDS_REVIEW").json()
        media_candidates = [item for item in candidates if item["media_id"] == media_id]
        assert media_candidates, "The audible event should produce a review candidate."
        top = max(media_candidates, key=lambda item: item["ranking_score"] or 0)
        assert top["ranking_score"] > 0.7, top
        assert top["start_seconds"] <= 21.0 <= top["end_seconds"], top
        ranking_components=json.loads(top["ranking_components_json"] or "{}")
        assert 20.0 <= ranking_components["event_time_seconds"] <= 22.0
        assert top["detector"] == "local_audio_motion_scene_ranker_v2"
        assert "uncalibrated" in top["explanation"].lower()
        assert ranking_components["visual_motion_samples_available"] is True
        # Analysis must never auto-approve or publish a candidate.
        assert all(item["state"] == "NEEDS_REVIEW" for item in media_candidates)
        assert client.get("/api/publishing/videos").json() == []
