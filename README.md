# Gaming Clip Factory AI

Local-first, rights-gated Windows software for producing gaming clips. This repository now contains a substantial development alpha: local API/dashboard, persistent jobs, metadata discovery, optional local transcription/vision, FFmpeg rendering/QC, thumbnails and credential-gated YouTube services. It is **not fully hands-off end-to-end on a fresh machine**: provider credentials, actual footage rights, optional model weights, a YouTube channel connection and local acceptance tests are still needed.

## What is implemented now

- Local app: FastAPI, SQLite numbered migrations 001–019, React/TypeScript/Vite dashboard, loopback API.
- Source discovery: Twitch Helix clip metadata and YouTube Data API public video metadata search, including persisted schedules. These store URLs/metadata only; they do not download platform footage. New sources always start with rights UNKNOWN.
- Rights registry: documented permission evidence, editing/commercial-use permission, owner review and expiry checks. Expired/revoked rights block processing and publishing.
- Local media intake: configured media-root containment, file checks, SHA-256 and duplicate protection.
- Analysis: FFprobe metadata and FFmpeg scene-change candidates. These are heuristics, not a semantic gameplay-event detector.
- Optional local speech recognition: Faster-Whisper produces transcript/timed segments. A deterministic transcript-grounded content director drafts format/title/description/tags and caption timing. Owners can also write an exact script and explicitly synthesize it through a configured local Piper CLI/voice model; generated WAVs are hashed, previewed and mixed into renders with gameplay-audio ducking. The app does not invent or auto-synthesize scripts.
- Optional local vision assessment: Ollama-compatible vision model samples a configurable 3 or 5 frames per candidate, passes candidate-relative timestamps to the model, and returns structured evidence. Scores are uncalibrated and never auto-approve; remote endpoints are blocked by default.
- Candidate review: approve/reject flow with transcript/vision evidence and a candidate-relative caption editor for timing/text corrections before rendering. **Opt-in supervised auto-production** on an explicitly configured authorized folder can automatically render up to three scene-change candidates per stable media file. The default stops at local rendering for owner review. As a separate explicit opt-in, a watch can automatically upload at most one QC-PASS clip per source recording as **private** after YouTube OAuth is connected and both the environment and dashboard upload gates are enabled. This never schedules public release; the owner must inspect the private upload before deciding whether to make it public. Manual candidate review and manual upload remain the default.
- Local rendering: H.264/AAC 1080×1920 MP4. Owner chooses fit-and-pad or blurred-backdrop composition; both preserve the full foreground gameplay frame without forcing a center crop. Timed caption sidecars are saved and may be burned in if the FFmpeg subtitle filter exists.
- Local poster and designed cover: the pipeline extracts a 720×1280 poster from the render, creates a title-led 9:16 JPEG Shorts cover from the actual frame, records hash/dimensions and uploads it with the official YouTube thumbnail API after processing succeeds. Cover generation/upload has its own visible status, retry path and failure handling; it does not falsify video-upload success.
- Automated QC: FFprobe checks dimensions/duration/codecs/audio; FFmpeg decode scan and black/frozen-frame heuristics produce PASS or REVIEW. REVIEW outputs may be previewed but only PASS outputs can be queued for YouTube.
- YouTube service: OAuth Desktop flow uses the OS keyring; a scope-version marker requires reconnect after scope changes. Private resumable uploads, processing-status polling/recheck, upload history, uncertain-outcome reconciliation and Analytics API snapshots exist behind local configuration.
- Analytics feedback: exact-window comparisons group observed outcomes by game and content format. A cohort is highlighted only with at least three comparable videos; smaller samples are labeled insufficient and no metrics are fabricated.
- Scheduled release controls: public release is separately gated, begins from a private upload, rechecks current rights/output hash/size/QC and live processing before making a video public; owners can cancel/reschedule. Global pause defers outbound YouTube operations. The worker only records `PUBLISHED` after YouTube's response or a follow-up API read confirms public visibility; uncertain outcomes require reconciliation.
- Idempotency and migrations: write transitions use immediate SQLite locks; a partial unique index allows one publication record per render. Migration numbers are validated as unique and each migration plus its version marker is applied atomically.

## Still not complete

- Public metadata search does not supply legal permission to republish footage. Owner must provide/prove rights or provide authorized local files; source media is not downloaded by current provider integrations.
- Scene-change and sparse-frame vision estimates do not reliably detect gameplay events across games. Vision scores are subjective and uncalibrated; game-specific accuracy needs labeled, rights-cleared validation clips.
- Transcript correction beyond candidate-relative captions, narration pronunciation/emotion/timing controls, richer templates/compositions, automated visual safe-area checks and advanced repair/quarantine remain incomplete. Local script-to-WAV synthesis is implemented but requires a configured Piper executable and voice. A custom cover is a deterministic title layout, not AI-generated artwork; the actual YouTube account must be eligible for custom thumbnail uploads.
- Remotion is not integrated; compatibility and licensing need review.
- No real provider credentials, creator permission evidence or YouTube channel has been validated end-to-end in this repository.
- The user's Windows installation/FFmpeg build still needs a local acceptance run; hosted CI is not a substitute.

## Requirements

- Windows 10/11, Python 3.11 or 3.12, Node.js 20.19+ or 22.12+, and npm.
- FFmpeg/FFprobe installed separately. The app auto-detects common project-local directories or uses GCF_FFMPEG_PATH/GCF_FFPROBE_PATH.
- Provider credentials only for integrations you enable.

No API credentials, OAuth files, model weights or FFmpeg binaries are committed. Review the actual FFmpeg license/notices; do not redistribute binaries without checking obligations.

## Windows quick start

**First run:** install Python 3.11 or 3.12 and Node.js 20.19+ or 22.12+, then double-click `scripts\start.bat`. The launcher creates `.env` from `.env.example` only if missing, repairs missing dependencies, builds the optimized dashboard, starts/reuses the loopback API, checks local readiness and opens the dashboard. The API runs as a background process; its logs are stored at `data\logs\backend.stdout.log` and `data\logs\backend.stderr.log`. It does not download FFmpeg, AI model weights, provider credentials or OAuth tokens. If FFmpeg/FFprobe are missing, the readiness panel reports degraded status and media processing remains unavailable until those tools are installed/configured.

**Later runs:** double-click `scripts\start.bat` again. For development only, use `scripts\run-backend.bat` and `scripts\run-dashboard.bat` separately to run the Vite development server. To stop the background app, use `scripts\stop.bat`.

Dashboard: http://127.0.0.1:8765; API: http://127.0.0.1:8765; API docs: http://127.0.0.1:8765/docs; liveness: http://127.0.0.1:8765/api/health; readiness report: http://127.0.0.1:8765/api/readiness.

## Storage safety

The worker checks free space on the database/output volume before folder scans or claiming another job. By default it pauses the global queue if free space falls below 3 GB, or if disk capacity cannot be checked. Configure `GCF_DISK_SAFETY_ENABLED` and `GCF_MIN_FREE_DISK_GB` in the local `.env`. When space recovers, the disk-specific alert clears but the global pause remains set until you explicitly resume in Operations. This protects local renders/model caches from filling the drive and avoids a silent restart loop.

## Source and rights workflow

1. Use Automation Setup to search Twitch/YouTube after configuring a provider. Search results enter the registry as UNKNOWN.
2. Record a license, contract or creator grant that allows the intended editing and commercial use. A public URL or API search result is not permission.
3. Only mark AUTHORIZED after evidence, editing/commercial flags and owner review are complete. Expired permissions fail closed.
4. Place authorized files within the media root and register their relative path.
5. Analyze the source; optionally run local transcription/vision. Scene-change and model-based results are estimates.
6. By default, review candidates/captions and approve or reject them individually. A watched folder can explicitly opt in to supervised auto-render (up to three scene-change candidates per stable file). A separate private-auto-upload option is disabled by default and requires connected YouTube OAuth plus both the environment and dashboard/database upload gates.
7. When private auto-upload is enabled, the first QC-PASS render from a source recording can be queued for private YouTube upload. The cap is one automatic upload per imported recording, rights and artifact integrity are rechecked, and no public-release job is created. QC REVIEW/failed renders remain local.
8. Inspect the finished clip and private YouTube upload before any public release. Public release remains separately gated and manually supervised; keep `GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING=false` during acceptance testing.

## Provider setup

### Twitch Helix discovery

Create a Twitch developer app. Set GCF_TWITCH_CLIENT_ID and GCF_TWITCH_CLIENT_SECRET in .env, restart backend and use Automation Setup → Twitch Helix. It discovers metadata only and does not change rights status.

### YouTube public metadata discovery

Enable YouTube Data API v3 in Google Cloud and set GCF_YOUTUBE_API_KEY for public search. An API key is not a channel login or reuse permission.

### YouTube OAuth/upload/analytics

1. Create a Google Cloud OAuth Desktop client and meet consent/testing requirements.
2. Save client JSON outside this repository.
3. Set GCF_YOUTUBE_CLIENT_SECRETS to its path.
4. From backend with the project virtual environment active, run python -m app.youtube_auth and authorize the intended channel in the browser.
5. Tokens are held in the OS keyring. If the required scopes change, reconnect by running the OAuth command again.
6. Use Automatic measurement to opt in to recurring Analytics sync (minimum interval 6 hours) and select a completed-day reporting window. The game/format report compares exact matching date windows and needs at least three videos before highlighting a cohort.
7. Test private upload, processing polls and reconciliation before setting GCF_ENABLE_YOUTUBE_UPLOADS=true and explicitly enabling uploads in the dashboard.
8. Keep GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING=false until supervised acceptance passes. This gate is independent of uploads.

Never put keys, OAuth JSON or tokens in GitHub issues, commits or chat. Quotas, channel/account restrictions, platform policy and source rights still apply.

## Optional local narration / voiceover

1. Install a local Piper CLI executable separately. Configure `GCF_PIPER_EXECUTABLE_PATH`, `GCF_PIPER_MODEL_PATH` (.onnx voice model), and optionally `GCF_PIPER_CONFIG_PATH` (matching .onnx.json) in your local .env.
2. Review the engine’s license and the individual voice model card/dataset license. Voice models have different terms, so verify commercial use/attribution before publishing. This repository does not bundle or download a voice or runtime.
3. Set `GCF_ENABLE_NARRATION=true` and restart the backend. In Candidate Review, edit and save an exact owner-reviewed script, generate the local voiceover, preview the resulting WAV, and then approve/render the candidate.
4. Editing the script invalidates earlier audio. The renderer checks the audio hash and will refuse to silently cut off voiceover longer than the candidate.

Voiceover is opt-in and remains local until you approve a clip for rendering. The system does not invent a narration script or silently add speech to a render.

## Optional local transcription

1. Run scripts\setup-ml-windows.bat.
2. Set GCF_ENABLE_TRANSCRIPTION=true.
3. Prefer GCF_WHISPER_MODEL_PATH to an existing local model. Otherwise set GCF_WHISPER_ALLOW_MODEL_DOWNLOAD=true explicitly.
4. Restart backend; transcription may be queued after media analysis. Inference is local and may be CPU-intensive.

## Optional local vision

1. Install Ollama and a compatible vision model after reviewing its license and hardware requirements.
2. Set GCF_ENABLE_VISION_ANALYSIS=true; default endpoint is http://127.0.0.1:11434/api/chat.
3. Restart backend and use Candidate Review → Analyze with vision, or let enabled media analysis queue assessments.
4. Scores are subjective and uncalibrated; sampled frames cannot reliably prove event timing. Candidates remain NEEDS_REVIEW.
5. Remote endpoints are blocked by default because sampled frames may be private. Only set GCF_VISION_ALLOW_REMOTE=true after privacy review.

## Test and CI

From project root:

    # Windows Python 3.12 example; choose the lock matching your OS and Python version.
    python -m pip install --require-hashes -r backend\requirements-lock-win-py312.txt
    python -m pytest -q

Frontend:

    cd frontend
    npm ci
    npm run build

Platform-specific hash-pinned backend/optional-ML locks, `frontend/package-lock.json`, `npm ci`, and Windows dependency-fingerprint checks are merged on `main` as `2946cba2ef683c61f0b61678c8fd83c97b349b3d`. The exact dependency-lock branch passed all six jobs in [run #411](https://github.com/zafuquu/GAMING-CLIP-FACTORY/actions/runs/40485109430), including the real one-click Windows startup/readiness/restart/stop smoke. The private-auto-upload feature is being validated on a separate branch based on this updated baseline. CI uses synthetic media and a mocked YouTube provider; it does not certify a real credentialed channel.

Hosted CI does not verify real credentials, actual source rights or the target Windows environment.

## Important limitations

- No public discovery endpoint is treated as a reuse/download grant.
- Public upload/release gates are disabled by default.
- Global pause defers new queue claims and checks before outbound YouTube mutations, but it does not instantly kill an already-running FFmpeg process.
- No general user authentication is implemented; do not expose the API/dashboard to LAN/internet.
- Single local worker only. Multi-worker deployment, quota budgets, notifications, quota budgets, notifications, and extensive Windows stress testing remain future work.

## Documentation

- docs/ARCHITECTURE.md
- docs/AUTOMATION_ROADMAP.md
- docs/IMPLEMENTATION_STATUS.md
- docs/DEPENDENCY_AUDIT.md
- docs/ENVIRONMENT_AUDIT.md
- docs/SECURITY.md
