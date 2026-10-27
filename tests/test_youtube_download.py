"""Tests for rights-gated yt-dlp acquisition and actual worker media registration."""
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings
from app.main import create_app
from app.worker import Worker
from app.integrations.youtube_download import (
    YouTubeVideoDownloader, YouTubeDownloadError, validate_youtube_video_url,
)


@pytest.mark.parametrize(("url", "expected"), [
    ("https://www.youtube.com/watch?v=abcdefghijk", "https://www.youtube.com/watch?v=abcdefghijk"),
    ("https://youtu.be/abcdefghijk?t=12", "https://www.youtube.com/watch?v=abcdefghijk"),
    ("https://youtube.com/shorts/abcdefghijk", "https://www.youtube.com/watch?v=abcdefghijk"),
])
def test_youtube_video_url_normalization(url, expected):
    assert validate_youtube_video_url(url) == expected


@pytest.mark.parametrize("url", [
    "http://youtube.com/watch?v=abcdefghijk",
    "https://evil.example/watch?v=abcdefghijk",
    "https://youtube.com/playlist?list=PL123456",
    "https://youtube.com/watch?v=abcdefghijk&list=PL123456",
    "https://youtube.com/channel/UC123456789",
    "https://youtube.com/watch?v=short",
])
def test_youtube_url_validation_rejects_non_video_and_non_https(url):
    with pytest.raises(ValueError):
        validate_youtube_video_url(url)


def test_ytdlp_invocation_is_single_video_bounded_and_has_no_browser_cookies(tmp_path, monkeypatch):
    import app.integrations.youtube_download as module

    calls = []
    target = tmp_path / "download.mp4"

    def fake_run(command, **kwargs):
        calls.append(command)
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, stdout="test-version\n", stderr="")
        template = command[command.index("--output") + 1]
        output = Path(template.replace(".%(ext)s", ".mp4"))
        output.write_bytes(b"synthetic video bytes")
        return subprocess.CompletedProcess(command, 0, stdout="downloaded", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    result = YouTubeVideoDownloader(executable="python", timeout_seconds=30).download(
        "https://youtu.be/abcdefghijk", target
    )
    command = calls[0]
    assert "--no-playlist" in command
    assert "--max-filesize" in command
    assert "--match-filter" in command
    assert "--cookies" not in command
    assert "--cookies-from-browser" not in command
    assert "--username" not in command
    assert target.read_bytes() == b"synthetic video bytes"
    assert result["provider"] == "yt-dlp"
    assert result["downloaded_bytes"] == len(b"synthetic video bytes")


def _setup_client(tmp_path):
    root = tmp_path / "project"
    data = root / "data"
    media = data / "media"
    cfg = Settings(root, data, media, data / "factory.sqlite3",
                   shutil.which("ffmpeg"), shutil.which("ffprobe"), 90)
    client = TestClient(create_app(settings=cfg, start_worker=False))
    client.__enter__()
    return client, cfg, media


def _add_source(client, authorize):
    response = client.post("/api/sources", json={
        "source_url": "https://www.youtube.com/watch?v=abcdefghijk",
        "platform": "youtube",
        "title": "Authorized gameplay test video",
        "creator": "Test creator",
        "game": "Test game",
    })
    assert response.status_code == 201, response.text
    sid = response.json()["id"]
    if authorize:
        rights = client.put(f"/api/sources/{sid}/rights", json={
            "status": "AUTHORIZED",
            "license_type": "Explicit authorization for automated test",
            "permission_evidence": "Synthetic test fixture",
            "editing_allowed": True,
            "commercial_use_allowed": True,
            "reviewed_by_owner": True,
        })
        assert rights.status_code == 200, rights.text
    return sid


def test_youtube_download_endpoint_blocks_unknown_rights(tmp_path):
    client, cfg, _media = _setup_client(tmp_path)
    try:
        sid = _add_source(client, authorize=False)
        response = client.post(f"/api/sources/{sid}/fetch-youtube-video")
        assert response.status_code == 409, response.text
        with sqlite3.connect(cfg.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM jobs WHERE job_type='DOWNLOAD_YOUTUBE_VIDEO'").fetchone()[0] == 0
    finally:
        client.__exit__(None, None, None)


def test_youtube_download_worker_registers_verified_video_and_allows_analysis(tmp_path, monkeypatch):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and FFprobe are required for the media acquisition integration test")
    monkeypatch.setenv("GCF_DISK_SAFETY_ENABLED", "false")
    client, cfg, media_root = _setup_client(tmp_path)
    try:
        sid = _add_source(client, authorize=True)
        fixture = tmp_path / "authorized-fixture.mp4"
        subprocess.run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
            "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-y", str(fixture),
        ], capture_output=True, text=True, timeout=60, check=True)

        class FakeDownloader:
            def __init__(self, **kwargs):
                pass

            def download(self, source_url, destination):
                assert source_url == "https://www.youtube.com/watch?v=abcdefghijk"
                shutil.copyfile(fixture, destination)
                return {"provider": "yt-dlp", "version": "test", "downloaded_bytes": destination.stat().st_size}

        import app.worker as worker_module
        monkeypatch.setattr(worker_module, "YouTubeVideoDownloader", FakeDownloader)
        queued = client.post(f"/api/sources/{sid}/fetch-youtube-video")
        assert queued.status_code == 202, queued.text
        job_id = queued.json()["id"]
        worker = Worker(cfg.database_path, ffmpeg, ffprobe, 90, media_root=media_root)
        with sqlite3.connect(cfg.database_path) as db:
            db.row_factory = sqlite3.Row
            job = dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        worker.download_youtube_video(job)

        media = client.get("/api/media").json()
        assert len(media) == 1
        assert media[0]["source_id"] == sid
        assert media[0]["file_size"] == fixture.stat().st_size
        assert media[0]["rights_status"] == "AUTHORIZED"
        assert Path(media[0]["original_path"]).is_file()
        assert Path(media[0]["probe"]["streams"][0]["codec_type"] == "video" and media[0]["original_path"]).is_file()
        analysis = client.post(f"/api/media/{media[0]['id']}/analyze")
        assert analysis.status_code == 202, analysis.text
        with sqlite3.connect(cfg.database_path) as db:
            db.row_factory = sqlite3.Row
            saved_job = dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        assert saved_job["status"] == "COMPLETED"
        summary = json.loads(saved_job["payload_json"])
        assert summary["download_provider"] == "yt-dlp"
        assert summary["duration_seconds"] == pytest.approx(6, abs=0.3)
    finally:
        client.__exit__(None, None, None)
