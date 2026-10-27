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
from app.integrations.twitch_download import TwitchClipDownloader, clip_slug_from_url, _cdn_url, TwitchClipDownloadError

TOKEN = {
    "client_id": "test-client",
    "access_token": "test-access",
    "refresh_token": "test-refresh",
    "expires_at": "2099-01-01T00:00:00+00:00",
    "scope": ["channel:manage:clips"],
    "user_id": "user-123",
}


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, chunks=None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}
        self._chunks = chunks or []
        self.closed = False

    def json(self):
        return self._body

    def iter_content(self, chunk_size=1024):
        yield from self._chunks

    def close(self):
        self.closed = True


class FakeTwitchSession:
    def __init__(self, bytes_body=b"fake-mp4-bytes", forbidden=False):
        self.bytes_body = bytes_body
        self.forbidden = forbidden
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/helix/clips"):
            return FakeResponse(body={"data": [{
                "id": "ClipSlug123",
                "broadcaster_id": "broadcaster-1",
                "broadcaster_name": "Authorized Channel",
                "title": "Authorized clip",
                "duration": 25.0,
            }]})
        if url.endswith("/helix/users"):
            return FakeResponse(body={"data": [{"id": "user-123"}]})
        if url.endswith("/helix/clips/downloads"):
            if self.forbidden:
                return FakeResponse(status=403, body={"error": "Forbidden"})
            return FakeResponse(body={"data": [{
                "clip_id": "ClipSlug123",
                "landscape_download_url": "https://clips-media-assets2.twitch.tv/signed-file.mp4",
                "portrait_download_url": None,
            }]})
        if url == "https://clips-media-assets2.twitch.tv/signed-file.mp4":
            return FakeResponse(
                headers={"Content-Type": "video/mp4", "Content-Length": str(len(self.bytes_body))},
                chunks=[self.bytes_body],
            )
        raise AssertionError(f"Unexpected URL: {url}")


def test_clip_slug_parser_accepts_twitch_clip_url_shapes():
    assert clip_slug_from_url("https://clips.twitch.tv/ClipSlug123") == "ClipSlug123"
    assert clip_slug_from_url("https://www.twitch.tv/mychannel/clip/ClipSlug123") == "ClipSlug123"
    assert clip_slug_from_url("https://www.twitch.tv/clip/ClipSlug123") == "ClipSlug123"


@pytest.mark.parametrize("value", [
    "https://www.youtube.com/watch?v=not-a-twitch-clip",
    "http://clips.twitch.tv/ClipSlug123",
    "https://clips.twitch.tv/",
    "https://clips.twitch.tv/ClipSlug123/extra",
])
def test_clip_slug_parser_rejects_non_twitch_or_invalid_urls(value):
    with pytest.raises(ValueError):
        clip_slug_from_url(value)


def test_download_url_host_allowlist_rejects_untrusted_cdn():
    assert _cdn_url("https://clips-media-assets2.twitch.tv/path/file.mp4").startswith("https://")
    with pytest.raises(TwitchClipDownloadError):
        _cdn_url("https://evil.example/file.mp4")
    with pytest.raises(TwitchClipDownloadError):
        _cdn_url("http://clips-media-assets2.twitch.tv/file.mp4")


def test_twitch_downloader_calls_official_api_and_downloads_to_file(tmp_path, monkeypatch):
    import app.integrations.twitch_download as mod

    monkeypatch.setattr(mod, "load_user_token", lambda _session: TOKEN)
    session = FakeTwitchSession(b"test-video-payload")
    target = tmp_path / "clip.mp4"
    result = TwitchClipDownloader(session=session).download(
        "https://clips.twitch.tv/ClipSlug123", target
    )
    assert target.read_bytes() == b"test-video-payload"
    assert result["clip_id"] == "ClipSlug123"
    assert result["broadcaster_id"] == "broadcaster-1"
    assert result["download_layout"] == "landscape"
    assert result["downloaded_bytes"] == len(b"test-video-payload")
    assert not target.with_suffix(".mp4.part").exists()
    endpoints = [call[0] for call in session.calls]
    assert "https://api.twitch.tv/helix/clips" in endpoints
    assert "https://api.twitch.tv/helix/users" in endpoints
    assert "https://api.twitch.tv/helix/clips/downloads" in endpoints


def test_twitch_downloader_fails_closed_when_channel_editor_permission_is_missing(tmp_path, monkeypatch):
    import app.integrations.twitch_download as mod

    monkeypatch.setattr(mod, "load_user_token", lambda _session: TOKEN)
    session = FakeTwitchSession(forbidden=True)
    with pytest.raises(mod.TwitchUserNotConfigured, match="broadcaster or a channel editor"):
        TwitchClipDownloader(session=session).download(
            "https://clips.twitch.tv/ClipSlug123", tmp_path / "blocked.mp4"
        )
    assert not (tmp_path / "blocked.mp4").exists()


def test_twitch_downloader_removes_partial_file_when_size_limit_is_exceeded(tmp_path, monkeypatch):
    import app.integrations.twitch_download as mod

    monkeypatch.setattr(mod, "load_user_token", lambda _session: TOKEN)
    session = FakeTwitchSession(b"x" * 128)
    target = tmp_path / "too-big.mp4"
    with pytest.raises(TwitchClipDownloadError, match="size limit"):
        TwitchClipDownloader(session=session, max_bytes=32).download(
            "https://clips.twitch.tv/ClipSlug123", target
        )
    assert not target.exists()
    assert not target.with_suffix(".mp4.part").exists()


def _setup_client(tmp_path):
    root = tmp_path / "project"
    data = root / "data"
    media = data / "media"
    cfg = Settings(root, data, media, data / "factory.sqlite3", shutil.which("ffmpeg"), shutil.which("ffprobe"), 90)
    client = TestClient(create_app(settings=cfg, start_worker=False))
    client.__enter__()
    return client, cfg, media


def _add_twitch_source(client, authorize=False):
    response = client.post("/api/sources", json={
        "source_url": "https://clips.twitch.tv/ClipSlug123",
        "platform": "twitch",
        "title": "Authorized test Twitch clip",
        "creator": "Authorized Channel",
        "game": "Test game",
    })
    assert response.status_code == 201, response.text
    sid = response.json()["id"]
    if authorize:
        rights = client.put(f"/api/sources/{sid}/rights", json={
            "status": "AUTHORIZED",
            "license_type": "Explicit permission for this test source",
            "permission_evidence": "Test authorization fixture",
            "editing_allowed": True,
            "commercial_use_allowed": True,
            "reviewed_by_owner": True,
        })
        assert rights.status_code == 200, rights.text
    return sid


def test_twitch_download_api_enforces_rights_before_queueing(tmp_path, monkeypatch):
    import app.twitch_user_auth as auth

    monkeypatch.setattr(auth, "load_user_token", lambda: TOKEN)
    client, cfg, _media = _setup_client(tmp_path)
    try:
        sid = _add_twitch_source(client, authorize=False)
        blocked = client.post(f"/api/sources/{sid}/fetch-twitch-clip")
        assert blocked.status_code == 409, blocked.text
        with sqlite3.connect(cfg.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM jobs WHERE job_type='DOWNLOAD_TWITCH_CLIP'").fetchone()[0] == 0
    finally:
        client.__exit__(None, None, None)


def test_twitch_download_worker_fetches_and_registers_real_verified_mp4(tmp_path, monkeypatch):
    import app.twitch_user_auth as auth
    import app.worker as worker_module

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and FFprobe are required for the media acquisition integration test")
    monkeypatch.setattr(auth, "load_user_token", lambda: TOKEN)
    monkeypatch.setenv("GCF_DISK_SAFETY_ENABLED", "false")

    client, cfg, media_root = _setup_client(tmp_path)
    try:
        sid = _add_twitch_source(client, authorize=True)
        input_video = tmp_path / "source-fixture.mp4"
        subprocess.run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=5",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=5",
            "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-y", str(input_video),
        ], capture_output=True, text=True, timeout=60, check=True)

        class FakeDownloader:
            def __init__(self):
                pass

            def download(self, source_url, destination):
                assert source_url == "https://clips.twitch.tv/ClipSlug123"
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(input_video, destination)
                return {
                    "clip_id": "ClipSlug123",
                    "broadcaster_id": "broadcaster-1",
                    "broadcaster_name": "Authorized Channel",
                    "title": "Authorized clip",
                    "duration": 5.0,
                    "download_layout": "landscape",
                    "downloaded_bytes": destination.stat().st_size,
                    "file_path": str(destination),
                }

        monkeypatch.setattr(worker_module, "TwitchClipDownloader", FakeDownloader)
        queued = client.post(f"/api/sources/{sid}/fetch-twitch-clip")
        assert queued.status_code == 202, queued.text
        job_id = queued.json()["id"]
        worker = Worker(cfg.database_path, ffmpeg, ffprobe, 90, media_root=media_root)
        assert worker.one() is True

        jobs = client.get("/api/jobs").json()
        job = next(row for row in jobs if row["id"] == job_id)
        assert job["status"] == "COMPLETED", job.get("error")
        assert job["job_type"] == "DOWNLOAD_TWITCH_CLIP"
        media = client.get("/api/media").json()
        asset = next(row for row in media if row["source_id"] == sid)
        assert asset["file_name"].startswith("twitch_ClipSlug123_")
        output = Path(asset["original_path"])
        assert output.is_file() and output.stat().st_size > 0
        assert asset["sha256"]
        assert asset["probe"]["streams"]
        summary = json.loads(job["payload_json"])
        assert summary["download_provider"] == "twitch_official_clips_api"
        assert summary["rights_rechecked"] is True
        assert 4.5 <= summary["duration_seconds"] <= 5.5
    finally:
        client.__exit__(None, None, None)
