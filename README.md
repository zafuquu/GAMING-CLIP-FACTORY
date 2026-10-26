# Gaming Clip Factory AI

A local-first, rights-gated gaming video production application for Windows. The current foundation includes FastAPI, SQLite persistence, a React/TypeScript dashboard, rights review, local media registration, persistent jobs, FFmpeg scene-change candidate detection, candidate review, vertical rendering, deterministic QC, and a global pause switch.

> **Current release: local production foundation.** This is not yet the fully autonomous content factory. Source discovery is manual, scene changes are not semantic highlight recognition, AI content planning/transcription/captions are not integrated, Remotion is not integrated, and YouTube OAuth/upload/scheduling/analytics are not configured. The UI reflects unavailable integrations honestly and does not fabricate performance data.

## Current capabilities

- SQLite schema with numbered incremental migrations.
- Manual source registration with duplicate URL protection.
- Rights registry. AUTHORIZED requires license description, permission evidence, editing/commercial-use permission, and owner review. Expired rights fail closed at source display, media import, queueing, and worker execution.
- Local media registration only inside the configured media root, with supported extension checks, SHA-256 identity and duplicate protection.
- Persistent SQLite job queue and bounded FFmpeg/FFprobe subprocess timeouts.
- FFprobe metadata extraction and FFmpeg scene-change windows. Scene changes are heuristics only; candidates require human review.
- Candidate review page to approve/reject candidates. Approval queues a render job only when current rights and FFmpeg tools are available.
- Basic portrait MP4 rendering at 1080x1920 using fit-and-pad layout (no forced center crop), H.264/AAC where source audio is present, unique outputs, SHA-256 and deterministic QC checks for dimensions, duration, streams and file size.
- Rendered outputs and QC metadata are stored in SQLite and shown in the dashboard.
- Dashboard reads actual database state; no seeded fake analytics.
- Local-only API binding by default (127.0.0.1).

## Requirements

- Python 3.11+ recommended.
- Node.js 20.19+ or 22.12+ and npm.
- FFmpeg and FFprobe for analysis/render jobs. The app checks tools/ffmpeg/ and ffmpeg-9.0.2-essentials_build/bin/, or accepts GCF_FFMPEG_PATH and GCF_FFPROBE_PATH overrides.
- Windows 10/11 is the primary target.

Review the actual FFmpeg build license and notices before redistribution. Gyan identifies its static builds as GPLv3-licensed. No FFmpeg binaries are committed here.

## Quick start — Windows

1. Run scripts\setup-windows.bat to create a virtual environment and install backend/frontend dependencies.
2. Start the backend with scripts\run-backend.bat.
3. In another terminal, start the dashboard with scripts\run-dashboard.bat.
4. Open the Vite URL printed by npm (normally http://127.0.0.1:5173). API: http://127.0.0.1:8765; API docs: http://127.0.0.1:8765/docs.

Manual setup:

    py -3 -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install -r backend\requirements.txt
    cd backend
    python -m app

In another terminal:

    cd frontend
    npm install
    npm run dev

## Local production workflow

1. Register a source in Sources & Rights. It starts UNKNOWN.
2. Review permission evidence. A public URL is not permission to reuse.
3. Mark the source AUTHORIZED only when the documented terms allow editing and commercial use and the owner has reviewed them. You can record an expiration time and later revoke/reject permission.
4. Put authorized footage in the configured media root (default data/media) and register its relative path in Media Library.
5. Queue analysis. FFmpeg generates scene-change candidate windows; this is not a semantic highlight classifier.
6. In Candidate Review, approve or reject each interval. Approval starts an FFmpeg portrait render and QC job. Expired/revoked permissions or global pause block it.
7. Inspect file path, render dimensions/duration, hash and QC status under Candidate Review. The current output is a technical cut/layout, not an AI-authored edit.

## Tests

From the repository root:

    python -m pip install -r backend\requirements.txt
    python -m pytest -q

Frontend checks:

    cd frontend
    npm install
    npm run build

GitHub Actions tests the backend on Windows/Linux with Python 3.11/3.12, runs an FFmpeg render/QC integration test on Linux, and typechecks/builds the dashboard.

## Not implemented yet

- Automated source/creator/game discovery providers.
- Semantic/game-specific event understanding and explainable highlight ranking beyond scene-change heuristics.
- Transcript-grounded content director, narration, speech recognition, captions and thumbnail generation.
- Advanced QC: black/frozen-frame detection, caption timing/readability, safe areas, automated repair, quarantine policy and preview player.
- Remotion integration and its licensing review.
- Official YouTube OAuth, uploads, upload reconciliation, scheduled publication, quota handling and analytics.
- Autonomous publishing. There is no publishing endpoint in this release.

## Safety boundaries

- No source downloading or external discovery provider is wired in.
- No YouTube OAuth or upload calls exist; autonomous public publishing is disabled.
- The global pause prevents new jobs from being queued/claimed. It does not forcibly terminate an already running subprocess.
- Media registration doesn't copy the input; it registers a file already under the media root.
- API binds to loopback. Do not expose it without authentication and a security review.

## Documentation

- docs/ARCHITECTURE.md
- docs/AUTOMATION_ROADMAP.md
- docs/IMPLEMENTATION_STATUS.md
- docs/DEPENDENCY_AUDIT.md
- docs/ENVIRONMENT_AUDIT.md
- docs/SECURITY.md
