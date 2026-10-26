# Gaming Clip Factory AI

Local-first, rights-gated Windows software for producing gaming clips. This repository has grown from a blank repo into a tested development alpha with a local API, dashboard, persistent jobs, metadata discovery providers, FFmpeg rendering/QC, optional local transcription and a credential-gated YouTube service. It is **not yet hands-off end-to-end on a fresh machine**: provider credentials, permission evidence, optional model weights and a YouTube channel connection must be configured, and semantic game-event recognition remains limited.

## What is implemented now

- **Local application:** FastAPI, SQLite with numbered migrations, React/TypeScript/Vite dashboard; API binds to 127.0.0.1.
- **Source discovery:** Twitch Helix game clip metadata and YouTube Data API public video metadata searches. These store URLs/metadata only; they do not download footage. Discovery records always start with rights UNKNOWN.
- **Rights registry:** explicit permission evidence, license/type, editing and commercial-use flags, owner review, expiration; expired/revoked rights block analysis/render/upload/public release.
- **Local media intake:** files must be under the configured media root; file extension, SHA-256 and duplicate checks are enforced.
- **Analysis:** FFprobe metadata and FFmpeg scene-change intervals. Scene changes are heuristics, not a semantic highlight detector.
- **Optional local speech recognition:** Faster-Whisper can produce transcript and timed word/phrase segments after explicit opt-in. A deterministic transcript-grounded content director suggests a format/title/description/tags and writes caption timing. It does not invent narration or assert that an event happened.
- **Candidate review:** owner can approve/reject candidate windows in the dashboard.
- **Local rendering:** approved intervals render as H.264/AAC 1080×1920 MP4 fit-and-pad output, preserving the full gameplay frame rather than forcing a center crop. When transcript caption timings exist and the FFmpeg subtitle filter is present, captions may be burned in; an SRT sidecar is also saved.
- **Automated QC:** verifies file size, probe/decode success, dimensions, duration, codecs, source audio preservation and scans for long fully-black frames/potential frozen frames. Warnings are REVIEW, not PASS. Only PASS outputs may be queued for YouTube.
- **YouTube service:** OAuth Desktop flow stores tokens in the OS keyring. Private resumable upload, upload history, uncertainty reconciliation, YouTube Analytics sync and a separately gated scheduled public-release worker are implemented behind local configuration. Upload starts private; public release is a separate explicit opt-in and rechecks permission, output hash and QC immediately before release.
- **Safety:** global pause, persistent jobs, bounded subprocess timeout/retries, restart recovery, uncertain upload reconciliation, no dummy analytics and no credentials in repository.

## Still not complete

- No platform currently provides automatic legal permission to republish the footage found in its public search results. Owner must supply/prove rights or provide authorized files; no source media is downloaded by this alpha.
- Scene-change + transcript heuristics do not reliably identify gameplay events (clutches, kills, strategy, funny moments) across games. A verified game-aware vision model/event classifier and score calibration still need implementation and labelled validation footage.
- AI narration, high-quality caption correction, thumbnails, diverse advanced compositions, safe-area validation and full template system remain incomplete.
- Remotion is not integrated; first review license/version fit for the intended use.
- No real credentials/channel has been set up or tested from this repository. YouTube integrations are credential-gated.
- Windows end-to-end validation using your exact local FFmpeg build is still required; CI uses hosted runners.

## Requirements

- Windows 10/11.
- Python 3.11+ (recommended).
- Node.js 22 LTS or compatible current supported Node 20+ and npm.
- FFmpeg/FFprobe. The app auto-detects the tools/ffmpeg folder and ffmpeg-9.0.2-essentials_build/bin folder, or use the explicit paths below.
- API credentials only for providers you enable.

No FFmpeg binaries or API credentials are committed. Review FFmpeg build license/notice files; Gyan identifies its static builds as GPLv3-licensed. Do not redistribute binaries without checking applicable obligations.

## Windows quick start

1. Copy .env.example to .env. Leave provider/model/upload gates disabled until you configure them.
2. Run scripts\setup-windows.bat.
3. Start backend with scripts\run-backend.bat.
4. In a second terminal run scripts\run-dashboard.bat.
5. Open http://127.0.0.1:5173 (if Vite prints a different local port use that URL). API: http://127.0.0.1:8765; interactive API docs: http://127.0.0.1:8765/docs.

The local data store defaults to data\gaming_clip_factory.sqlite3, media intake to data\media, and render outputs to data\outputs. Keep local media, SQLite database, .env, OAuth JSON and model cache out of Git.

### FFmpeg paths

The app looks for tools\ffmpeg\ffmpeg.exe and tools\ffmpeg\ffprobe.exe, then the manually extracted ffmpeg-9.0.2-essentials_build\bin path. If your extracted directory uses another name, set GCF_FFMPEG_PATH and GCF_FFPROBE_PATH in your local .env.

## Source and rights workflow

1. **Discovery**: use Automation Setup to search Twitch and/or YouTube metadata after configuring a provider. Results enter Sources & Rights with status UNKNOWN.
2. **Permission review**: document a license/grant/contract or actual creator permission that allows the contemplated editing and commercial use. Public availability, a high view count or a public API response is not proof.
3. **Authorize only verified sources**: include evidence, license description, editing/commercial flags, owner-review confirmation and expiration where relevant. The API rejects incomplete authorization; expired permission fails closed.
4. **Intake**: put the authorized file in the media root and register its relative path.
5. **Analysis**: generate scene-change windows and optionally local transcript/timed captions.
6. **Candidate Review**: review the window and evidence; approve or reject it.
7. **Render/QC**: approve queues FFmpeg rendering. PASS can proceed; REVIEW outputs should be inspected before any further use.
8. **YouTube**: connect your channel and enable uploads only after setup. The default result is private. Public scheduling requires a separate environment gate and just-in-time rights/QC checks. A network uncertainty requires manual reconciliation instead of an automatic duplicate retry.

## Provider setup

### Twitch Helix metadata discovery

Create an application in the Twitch Developer Console. Put GCF_TWITCH_CLIENT_ID and GCF_TWITCH_CLIENT_SECRET in .env, restart the backend, and use **Automation Setup → Twitch Helix**. It searches categories and clips metadata. It never downloads source media and never changes the rights status from UNKNOWN.

### YouTube public metadata discovery

Create a Google Cloud project and enable YouTube Data API v3. You can set GCF_YOUTUBE_API_KEY in .env for **public metadata search only**. An API key is not a channel login and cannot upload videos. Search results are still UNKNOWN rights.

### YouTube OAuth/upload/analytics

1. In Google Cloud Console, create an OAuth client with Desktop app type and meet applicable consent/testing requirements.
2. Save its client JSON outside the repository, e.g. in your private user documents folder.
3. Set GCF_YOUTUBE_CLIENT_SECRETS to that file path in local .env.
4. From the backend directory with the project virtual environment active, run python -m app.youtube_auth. Authorize the intended channel in the browser. Tokens are stored with Windows OS keyring, not printed or committed.
5. Use **Automation Setup → Check channel connection**. Private upload remains off by default.
6. After you've tested private uploads and reconciliation, set GCF_ENABLE_YOUTUBE_UPLOADS=true in local .env, restart backend and explicitly enable uploads in the dashboard.
7. Keep GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING=false until supervised, private uploads and current-rights tests pass. Public publication is deliberately a second independent gate.

API quotas, YouTube account/channel restrictions, credential verification, public publishing rules and whether a source can legally be reused still apply. Do not put any key, OAuth JSON or token in a GitHub issue, commit or chat.

## Optional local transcription

1. Run scripts\setup-ml-windows.bat.
2. Set GCF_ENABLE_TRANSCRIPTION=true.
3. Prefer GCF_WHISPER_MODEL_PATH to an already downloaded local model directory. Alternatively set GCF_WHISPER_ALLOW_MODEL_DOWNLOAD=true to explicitly allow the optional model download. Check model/dependency licensing and use CPU int8 first unless you have verified compatible GPU support.
4. Restart the backend. Media analysis will queue local transcription after scene analysis, persist timed segments and produce a draft content plan. Human review is still required.

## Test and CI

From project root:

    python -m pip install -r backend\requirements.txt
    python -m pytest -q

Frontend:

    cd frontend
    npm install
    npm run build

The GitHub Actions workflow runs backend tests on Windows/Linux with Python 3.11/3.12, a Linux FFmpeg render integration test and a frontend typecheck/production build. CI passing is not a substitute for testing your exact local Windows FFmpeg package and real provider credentials.

## Important limitations

- No public discovery endpoint is treated as an authorization or download grant.
- Public publishing is not enabled by default.
- The worker does not terminate an active subprocess immediately when the dashboard pause is pressed; FFmpeg commands have finite timeouts, and pause prevents new queued work from starting.
- Do not expose the dashboard/API to your LAN or the internet; no general user authentication has been implemented. Keep it on loopback.
- A single local worker is used initially. Multi-machine deployment, high concurrency and PostgreSQL remain future work.

## Documentation

- docs/ARCHITECTURE.md
- docs/AUTOMATION_ROADMAP.md
- docs/IMPLEMENTATION_STATUS.md
- docs/DEPENDENCY_AUDIT.md
- docs/ENVIRONMENT_AUDIT.md
- docs/SECURITY.md
