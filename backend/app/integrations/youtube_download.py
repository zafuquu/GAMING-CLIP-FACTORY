"""Bounded yt-dlp acquisition for YouTube videos with documented reuse permission.

This adapter deliberately accepts only ordinary YouTube video URLs. It does not
read browser cookies, pass account credentials, fetch playlists/channels, or try
to defeat access controls. The caller must enforce the app's AUTHORIZED rights gate.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs


class YouTubeDownloadError(RuntimeError):
    pass


def validate_youtube_video_url(value: str) -> str:
    """Return a normalized ordinary YouTube video URL or fail closed."""
    try:
        parsed = urlparse(str(value).strip())
    except Exception as exc:
        raise ValueError("Enter a valid YouTube video URL.") from exc
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Only HTTPS YouTube video URLs are supported.")
    host = parsed.hostname.lower().rstrip(".")
    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtube-nocookie.com"}:
        raise ValueError("Only youtube.com and youtu.be video URLs are supported.")
    video_id = ""
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
    else:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        else:
            match = re.fullmatch(r"/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})/?", parsed.path)
            video_id = match.group(1) if match else ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("URL must identify one YouTube video, not a playlist, channel, or search page.")
    return "https://www.youtube.com/watch?v=" + video_id


class YouTubeVideoDownloader:
    def __init__(self, executable: str | None = None, timeout_seconds: int = 1800,
                 max_bytes: int = 2 * 1024 * 1024 * 1024, max_duration_seconds: int = 4 * 60 * 60):
        self.executable = executable or sys.executable
        self.timeout_seconds = max(30, min(int(timeout_seconds), 3600))
        self.max_bytes = max(1, int(max_bytes))
        self.max_duration_seconds = max(1, int(max_duration_seconds))

    def download(self, source_url: str, destination: Path) -> dict:
        normalized = validate_youtube_video_url(source_url)
        destination = Path(destination).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Fixed output path and one URL only. No cookies, browser profile, login,
        # playlist expansion, live-from-start, geo bypass, or shell invocation.
        template = str(destination.with_suffix("")) + ".%(ext)s"
        command = [
            self.executable, "-m", "yt_dlp",
            "--no-playlist", "--no-warnings", "--no-progress",
            "--restrict-filenames", "--max-filesize", str(self.max_bytes),
            "--match-filter", f"duration <= {self.max_duration_seconds}",
            "--format", "bv*+ba/b",
            "--merge-output-format", "mp4",
            "--output", template,
            "--no-simulate", normalized,
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=self.timeout_seconds, check=False, shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            self._cleanup(destination)
            raise YouTubeDownloadError("yt-dlp exceeded the configured download timeout.") from exc
        except OSError as exc:
            self._cleanup(destination)
            raise YouTubeDownloadError("Could not start yt-dlp. Install the backend requirements and restart the app.") from exc
        candidates = [p for p in destination.parent.glob(destination.stem + ".*")]
        candidates = [p for p in candidates if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"} and p.is_file()]
        if completed.returncode != 0 or not candidates:
            self._cleanup(destination)
            detail = (completed.stderr or completed.stdout or "yt-dlp did not produce a video file").strip()
            raise YouTubeDownloadError("yt-dlp download failed: " + detail[-1800:])
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        actual = candidates[0]
        try:
            size = actual.stat().st_size
            if size <= 0 or size > self.max_bytes:
                raise YouTubeDownloadError("Downloaded file is empty or exceeds the configured maximum size.")
            if actual != destination:
                if destination.exists():
                    destination.unlink()
                actual.replace(destination)
            return {
                "source_url": normalized,
                "file_path": str(destination),
                "file_name": destination.name,
                "downloaded_bytes": destination.stat().st_size,
                "provider": "yt-dlp",
                "version": self._version(),
            }
        except Exception:
            self._cleanup(destination)
            raise
        finally:
            for candidate in candidates:
                if candidate != destination:
                    try:
                        candidate.unlink(missing_ok=True)
                    except OSError:
                        pass

    def _version(self) -> str | None:
        try:
            result = subprocess.run(
                [self.executable, "-m", "yt_dlp", "--version"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10, shell=False,
            )
            return (result.stdout or "").strip()[:80] if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _cleanup(self, destination: Path) -> None:
        destination = Path(destination)
        for candidate in destination.parent.glob(destination.stem + ".*"):
            try:
                if candidate.is_file():
                    candidate.unlink(missing_ok=True)
            except OSError:
                pass
