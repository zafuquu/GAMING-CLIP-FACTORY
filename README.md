# Gaming Clip Factory AI

A local-first, rights-gated gaming video production control plane for Windows. This repository starts with a working foundation: FastAPI, SQLite persistence, a React/TypeScript dashboard, source-rights review, safe local media registration, a durable analysis queue, FFmpeg scene-change candidate detection, and a global pause switch.

> **Current release: foundation / development.** This is not yet the fully autonomous content factory in the master specification. Source discovery is manual, semantic highlight classification is not implemented, Remotion rendering is not integrated, and YouTube OAuth/upload/scheduling/analytics API calls are not configured. The UI reflects unavailable integrations honestly and does not fabricate performance data.

## Current capabilities

- Persistent SQLite schema with an initial migration.
- Manual source registration with duplicate URL protection.
- Explicit rights registry. A source becomes `AUTHORIZED` only after the owner supplies permission evidence, license description, editing and commercial-use permission, and an owner-review flag.
- Media registration only from the configured local media root and only after authorization.
- SHA-256 content identity for imported files.
- SQLite-backed jobs, bounded retries, process timeouts for media commands, and restart-persistent state.
- Local FFprobe metadata extraction and FFmpeg scene-change window candidates. Scene changes are only heuristics, not proof of exciting gameplay; candidates are marked for human review.
- Dashboard data comes from the actual local database; no seeded fake analytics.
- Local-only server binding by default (`127.0.0.1`).

## Requirements

- Python 3.11+ recommended.
- Node.js 20.19+ or 22.12+ and npm for the dashboard.
- FFmpeg and FFprobe for analysis jobs. The app checks `tools/ffmpeg/` and the extracted `ffmpeg-9.0.2-essentials_build/bin/` directory, or accepts `GCF_FFMPEG_PATH` and `GCF_FFPROBE_PATH` overrides.
- Windows 10/11 is the primary target.

Review FFmpeg's actual build license and notices before redistribution. Gyan's static builds are identified by the provider as GPLv3-licensed. No FFmpeg binaries are committed here.

## Quick start — Windows

1. Run `scripts\setup-windows.bat` to create a virtual environment and install backend/frontend dependencies.
2. Start the backend with `scripts\run-backend.bat`.
3. In another terminal, start the dashboard with `scripts\run-dashboard.bat`.
4. Open the Vite URL printed by npm (normally `http://127.0.0.1:5173`). The API is at `http://127.0.0.1:8765`; API docs are at `http://127.0.0.1:8765/docs`.

For manual setup, see the commands below:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
cd backend
python -m app
```

In another terminal:

```powershell
cd frontend
npm install
npm run dev
```

## Tests

From the repository root:

```powershell
python -m pip install -r backend\requirements.txt
python -m pytest -q
```

Frontend checks:

```powershell
cd frontend
npm install
npm run build
```

## Rights workflow

1. Add a source URL in **Sources & Rights**. New sources always start as `UNKNOWN`.
2. Independently document a reliable permission or license source. Public accessibility alone is not permission.
3. Use the owner-review form to set `AUTHORIZED` only when the documented permission allows editing and commercial use and has been reviewed. Otherwise leave the source pending or reject it.
4. Put a local, authorized file under the configured media root and register its relative path in **Media**.
5. Queue scene-change analysis. Candidate windows remain `NEEDS_REVIEW`; this release does not claim scene changes are semantic highlights.

## Safety and status boundaries

- No external discovery provider is wired in this release; add sources manually.
- No YouTube OAuth credentials or upload calls exist in this release. Publishing endpoints are intentionally absent, and autonomous public publishing is refused.
- The global pause stops the worker from claiming new queued jobs and blocks new analysis jobs. It does not forcibly terminate a command already running; running media commands have finite timeouts.
- Media import does not copy files. It registers an existing file within the configured media root, avoiding accidental copies or path traversal.
- The API binds to loopback by default. Do not expose it to a network without authentication and a security review.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Implementation status](docs/IMPLEMENTATION_STATUS.md)
- [Dependency audit](docs/DEPENDENCY_AUDIT.md)
- [Environment audit](docs/ENVIRONMENT_AUDIT.md)
- [Security model](docs/SECURITY.md)
