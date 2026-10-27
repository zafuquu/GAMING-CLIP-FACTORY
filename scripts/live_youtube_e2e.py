"""One-off live YouTube -> analysis -> Remotion Shorts acceptance run.

This is intentionally opt-in CI, not part of the regular unit-test suite. It downloads
only the owner-authorized URL supplied by the workflow, uses an isolated temporary DB,
and emits a JSON report without uploading the source or rendered video as an artifact.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings
from app.main import create_app
from app.worker import Worker


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def wait_job(client, worker, job_id: str, timeout: int = 3600) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        worker.one()
        rows = client.get("/api/jobs").json()
        row = next((item for item in rows if item.get("id") == job_id), None)
        if row and row["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            if row["status"] != "COMPLETED":
                raise RuntimeError(
                    f"Job {job_id} ended {row['status']}: {row.get('error', '')}"
                )
            return row
        time.sleep(0.5)
    raise TimeoutError(f"Timed out waiting for job {job_id}")


def main() -> int:
    video_url = os.environ.get("GCF_E2E_YOUTUBE_URL", "").strip()
    require(video_url.startswith("https://"), "GCF_E2E_YOUTUBE_URL must be an HTTPS URL.")
    require(
        os.environ.get("GCF_E2E_OWNER_CONFIRMED", "").strip().lower() == "true",
        "Explicit owner authorization is required; set GCF_E2E_OWNER_CONFIRMED=true only for content you own or are authorized to edit.",
    )
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    node = shutil.which("node")
    require(ffmpeg and ffprobe and node, "Node.js, FFmpeg, and FFprobe must be installed.")
    require(
        (ROOT / "remotion" / "node_modules" / "@remotion" / "renderer" / "package.json").is_file(),
        "Remotion dependencies are missing; install remotion/package.json dependencies first.",
    )

    os.environ.update({
        "GCF_DISK_SAFETY_ENABLED": "false",
        "GCF_RENDER_ENGINE": "remotion",
        "GCF_PROJECT_ROOT": str(ROOT),
        "GCF_ENABLE_YOUTUBE_UPLOADS": "false",
        "GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING": "false",
        "GCF_REMOTION_TIMEOUT_SECONDS": "1800",
        "GCF_YTDLP_TIMEOUT_SECONDS": "3600",
        "GCF_YTDLP_MAX_BYTES": str(2 * 1024 * 1024 * 1024),
        "GCF_YTDLP_MAX_DURATION_SECONDS": "14400",
    })

    report: dict = {
        "test": "live_youtube_download_analysis_remotion_render",
        "video_id": parse_qs(urlparse(video_url).query).get("v", ["unknown"])[0],
        "upload_or_publication_attempted": False,
        "steps": {},
    }
    with tempfile.TemporaryDirectory(prefix="gcf-live-e2e-", ignore_cleanup_errors=True) as temp:
        root = Path(temp) / "project"
        data = root / "data"
        media_root = data / "media"
        cfg = Settings(
            root, data, media_root, data / "factory.sqlite3", ffmpeg, ffprobe, 1800
        )
        with __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(
            create_app(settings=cfg, start_worker=False)
        ) as client:
            source = client.post("/api/sources", json={
                "source_url": video_url,
                "platform": "youtube",
                "title": "Owner-authorized live E2E test",
                "creator": "Video owner",
                "game": "Gameplay",
            })
            require(source.status_code == 201, f"Source creation failed: {source.text}")
            sid = source.json()["id"]
            rights = client.put(f"/api/sources/{sid}/rights", json={
                "status": "AUTHORIZED",
                "license_type": "Owner-provided authorization for this acceptance test",
                "permission_evidence": "Owner explicitly confirmed authorization for this run through the workflow input.",
                "editing_allowed": True,
                "commercial_use_allowed": True,
                "reviewed_by_owner": True,
            })
            require(rights.status_code == 200, f"Rights authorization failed: {rights.text}")

            queued = client.post(f"/api/sources/{sid}/fetch-youtube-video")
            require(queued.status_code == 202, f"Download was not queued: {queued.text}")
            worker = Worker(cfg.database_path, ffmpeg, ffprobe, 1800, media_root=media_root)
            download_job = wait_job(client, worker, queued.json()["id"], timeout=3600)
            report["steps"]["download"] = {
                "status": download_job["status"],
                "provider": "yt-dlp",
                "job_id": download_job["id"],
            }

            media_rows = client.get("/api/media").json()
            media = next((item for item in media_rows if item.get("source_id") == sid), None)
            require(media is not None, "Download job completed but no media asset was registered.")
            original = Path(media["original_path"])
            require(original.is_file() and original.stat().st_size > 0, "Downloaded file is missing or empty.")
            probe = subprocess.run(
                [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(original)],
                capture_output=True, text=True, timeout=60, check=True,
            )
            probe_data = json.loads(probe.stdout)
            streams = probe_data.get("streams", [])
            video_stream = next((x for x in streams if x.get("codec_type") == "video"), None)
            require(video_stream is not None, "Downloaded file has no video stream.")
            duration = float(probe_data.get("format", {}).get("duration", 0))
            require(duration > 0, "Downloaded file duration could not be verified.")
            report["steps"]["download"]["file_bytes"] = original.stat().st_size
            report["steps"]["download"]["duration_seconds"] = round(duration, 3)
            report["steps"]["download"]["video"] = {
                "codec": video_stream.get("codec_name"),
                "width": video_stream.get("width"),
                "height": video_stream.get("height"),
            }
            report["steps"]["download"]["audio_present"] = any(x.get("codec_type") == "audio" for x in streams)

            analyze = client.post(f"/api/media/{media['id']}/analyze")
            require(analyze.status_code == 202, f"Analysis was not queued: {analyze.text}")
            analysis_job = wait_job(client, worker, analyze.json()["id"], timeout=3600)
            candidates = [
                item for item in client.get("/api/candidates").json()
                if item.get("media_id") == media["id"] and item.get("state") == "NEEDS_REVIEW"
            ]
            require(candidates, "Real analyzer completed but generated no reviewable highlight candidates.")
            candidates.sort(key=lambda item: float(item.get("ranking_score") or 0), reverse=True)
            candidate = candidates[0]
            report["steps"]["analysis"] = {
                "status": analysis_job["status"],
                "candidate_count": len(candidates),
                "selected_candidate": {
                    "id": candidate["id"],
                    "start_seconds": candidate["start_seconds"],
                    "end_seconds": candidate["end_seconds"],
                    "ranking_score": candidate.get("ranking_score"),
                    "detector": candidate.get("detector"),
                },
                "note": "Signal rank is a review heuristic, not a calibrated virality prediction.",
            }

            decision = client.post(f"/api/candidates/{candidate['id']}/decision", json={
                "decision": "approve",
                "notes": "Owner-authorized acceptance test; verify render mechanics, not highlight quality.",
                "render_profile": "fit_pad",
            })
            require(decision.status_code == 202, f"Candidate approval/render queue failed: {decision.text}")
            render_job = wait_job(client, worker, decision.json()["job_id"], timeout=3600)
            renders = client.get("/api/renders").json()
            rendered = next((item for item in renders if item.get("candidate_id") == candidate["id"]), None)
            require(rendered is not None, "Render job completed but no render record was registered.")
            require(rendered.get("qc_status") == "PASS", f"Render QC did not pass: {rendered.get('qc_report')}")
            output = Path(rendered["output_path"])
            require(output.is_file() and output.stat().st_size > 1000, "Rendered Short is missing or too small.")
            final_probe = subprocess.run(
                [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
                capture_output=True, text=True, timeout=60, check=True,
            )
            final_data = json.loads(final_probe.stdout)
            final_streams = final_data.get("streams", [])
            final_video = next((x for x in final_streams if x.get("codec_type") == "video"), None)
            final_audio = next((x for x in final_streams if x.get("codec_type") == "audio"), None)
            final_duration = float(final_data.get("format", {}).get("duration", 0))
            require(final_video is not None, "Rendered output has no video stream.")
            require((final_video.get("width"), final_video.get("height")) == (1080, 1920),
                    f"Rendered output is not 1080x1920: {final_video.get('width')}x{final_video.get('height')}")
            require(final_audio is not None, "Rendered output has no audio stream.")
            require(3 <= final_duration <= 180, f"Rendered duration is outside Shorts-safe bounds: {final_duration}")
            report["steps"]["render"] = {
                "status": render_job["status"],
                "qc_status": rendered["qc_status"],
                "file_bytes": output.stat().st_size,
                "duration_seconds": round(final_duration, 3),
                "resolution": f"{final_video['width']}x{final_video['height']}",
                "video_codec": final_video.get("codec_name"),
                "audio_codec": final_audio.get("codec_name"),
                "render_engine": rendered.get("qc_report", {}).get("render_engine"),
                "ffmpeg_decode_scan": rendered.get("qc_report", {}).get("checks", {}).get("ffmpeg_decode_scan"),
                "output_file": "verified (temporary; not uploaded as artifact)",
            }

            with sqlite3.connect(cfg.database_path) as db:
                jobs = db.execute("SELECT job_type,status FROM jobs").fetchall()
            report["steps"]["jobs"] = [{"type": kind, "status": status} for kind, status in jobs]
            report["result"] = "PASS"

    report_path = Path(os.environ.get("GCF_E2E_REPORT_PATH", "live-youtube-e2e-report.json"))
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        failure = {
            "test": "live_youtube_download_analysis_remotion_render",
            "video_id": parse_qs(urlparse(os.environ.get("GCF_E2E_YOUTUBE_URL", "")).query).get("v", ["unknown"])[0],
            "result": "FAIL",
            "error_type": type(exc).__name__,
            "error": str(exc)[:3000],
        }
        Path(os.environ.get("GCF_E2E_REPORT_PATH", "live-youtube-e2e-report.json")).write_text(
            json.dumps(failure, indent=2), encoding="utf-8"
        )
        print(json.dumps(failure, indent=2), file=sys.stderr)
        raise
