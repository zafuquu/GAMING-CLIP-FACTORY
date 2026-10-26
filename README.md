# Gaming Clip Factory AI

Local-first, rights-gated Windows software for producing gaming clips. This repository has grown from a blank repo into a tested development alpha with a local API, dashboard, persistent jobs, metadata discovery providers, FFmpeg rendering/QC, optional local transcription/vision, and a credential-gated YouTube service. It is not fully hands-off end-to-end on a fresh machine: provider credentials, permission evidence, optional model weights, a YouTube channel connection and local acceptance tests are still needed.

## What is implemented now

- Local app: FastAPI, SQLite migrations 001–007, React/TypeScript/Vite dashboard, loopback API.
- Source discovery: Twitch Helix clip metadata and YouTube Data API public video metadata searches, including schedules. These store URLs/metadata only; no platform footage is downloaded. New sources always start with rights UNKNOWN.
- Rights registry: permission evidence, editing and commercial-use permissions, owner review and expiration; expired/revoked rights block processing and publishing.
- Local media intake: configured media-root containment, file checks, SHA-256 and duplicate protection.
- Analysis: FFprobe metadata and FFmpeg scene-change candidate windows. These are heuristics, not a semantic gameplay-event detector.
- Optional local speech recognition: Faster-Whisper can generate transcripts and timed phrase segments. A deterministic transcript-grounded content director drafts format/title/description/tags and caption timing; it does not invent narration.
- Optional local vision assessment: Ollama-compatible vision model samples three frames per candidate and returns structured event/evidence estimates. Scores are uncalibrated and never auto-approve candidates. It is disabled by default and blocks remote endpoints unless explicitly overridden.
- Candidate review: dashboard approval/rejection flow with transcript/vision evidence and a caption editor for relative timing/text corrections before rendering.
- Local rendering: approved cuts render as 1080x1920 H.264/AAC MP4 in fit-and-pad layout to preserve the full gameplay frame. Timed caption sidecars are saved and may be burned in if the FFmpeg subtitle filter exists.
- Automated QC: probe/decode, dimensions, duration, codecs/audio and black/frozen-frame heuristics. Issues become REVIEW, not PASS. Only PASS outputs are publishable.
- YouTube service: OAuth Desktop flow uses the OS keyring, with a scope-version marker requiring reconnect if scopes change. Private resumable upload, processing-status polling/recheck, upload history, uncertain-outcome reconciliation and Analytics API snapshots exist behind local configuration.
- Analytics feedback: same-window comparisons group observed results by game and content format. A group is highlighted only with at least three comparable videos; smaller samples are labeled insufficient and no metrics are fabricated.
- Scheduled release controls: public release is separately gated, stages video as private, rechecks rights/output hash/size/QC and live processing state before changing it to public; owners can cancel or reschedule release. Global pause defers outbound YouTube operations.
- Idempotency: key SQLite transitions use immediate write locks, and a partial unique index allows only one publication record per rendered output.

## Still not complete

- Public metadata search does not supply legal permission to republish footage. Owner must provide/prove rights or provide authorized local files; current providers do not download source media.
- Scene-change and sparse-frame vision estimates do not reliably detect gameplay events across games. Vision scores are subjective and uncalibrated; game-specific accuracy needs labeled, rights-cleared validation clips.
- Transcript correction/review, high-quality narration, thumbnail generation, richer template/composition system, safe-area checks and advanced repair/quarantine remain incomplete.
- Remotion is not integrated; compatibility and licensing need review.
- No real provider credentials, creator permission evidence or YouTube channel has been validated end-to-end in this repository.
- The user's own Windows installation/FFmpeg build still needs a local acceptance run; CI on hosted runners is not a substitute.

## Requirements

- Windows 10/11, Python 3.11+, Node.js/npm.
- FFmpeg/FFprobe installed separately. The app auto-detects common project-local directories or uses GCF_FFMPEG_PATH/GCF_FFPROBE_PATH.
- Provider credentials only for providers you enable.

No API credentials, OAuth files, model weights or FFmpeg binaries are committed. Review actual FFmpeg license/notices; do not redistribute binaries without checking obligations.

## Windows quick start

1. Copy .env.example to .env and keep optional providers/model/public-publishing gates disabled until configured.
2. Run scripts\setup-windows.bat.
3. Start backend with scripts\run-backend.bat.
4. In a second terminal run scripts\run-dashboard.bat.
5. Open http://127.0.0.1:5173 (or the Vite URL shown). API: http://127.0.0.1:8765; docs: http://127.0.0.1:8765/docs.

Defaults: data\gaming_clip_factory.sqlite3, media in data\media, render outputs in data\outputs. Keep .env, media/output files, OAuth client JSON and model caches out of Git.

## Source and rights workflow

1. Use Automation Setup to search Twitch/YouTube after configuring a provider. Search results enter the registry as UNKNOWN.
2. Record a license, contract or creator grant that actually allows the intended editing and commercial use. A public URL or API search result is not permission.
3. Only mark AUTHORIZED after evidence, editing/commercial flags and owner review are complete. Expired permissions fail closed.
4. Place authorized files within the media root and register their relative path.
5. Analyze the source; optionally run local transcription/vision. Scene-change and VLM results are estimates.
6. Review candidates, then approve/reject.
7. Inspect render/QC. REVIEW outputs may be previewed but cannot be uploaded; only PASS qualifies.
8. For YouTube, connect the intended channel and test private upload, processing status and reconciliation. Uploads start private. Public releases require a separate environment gate and current-rights/QC/hash checks at release time.

## Provider setup

### Twitch Helix discovery

Create a Twitch developer app. Set GCF_TWITCH_CLIENT_ID and GCF_TWITCH_CLIENT_SECRET in .env, restart backend and use Automation Setup → Twitch Helix. It discovers metadata only and does not change rights status.

### YouTube public metadata discovery

Enable YouTube Data API v3 in Google Cloud and set GCF_YOUTUBE_API_KEY for public search. An API key is not a channel login or reuse permission.

### YouTube OAuth/upload/analytics

1. Create a Google Cloud OAuth Desktop client and meet consent/testing requirements.
2. Save client JSON outside this repo.
3. Set GCF_YOUTUBE_CLIENT_SECRETS to its path.
4. From backend with the project venv active, run python -m app.youtube_auth and authorize the intended channel in the browser.
5. Tokens are held in the OS keyring. If the required scopes changed, reconnect by running the OAuth command again.
6. Use Automatic measurement to opt in to recurring Analytics sync (minimum interval 6 hours) and select a completed-day reporting window. The game/format report compares only exact matching date windows and requires at least three videos before highlighting a cohort.
6. Test private upload, processing polls and reconciliation before setting GCF_ENABLE_YOUTUBE_UPLOADS=true and explicitly enabling uploads in the dashboard.
7. Keep GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING=false until supervised acceptance passes. This gate is independent of uploads.

Never put keys, OAuth JSON or tokens into GitHub issues, commits or chat. Quotas, channel/account restrictions, platform policy and source rights still apply.

## Optional local transcription

1. Run scripts\setup-ml-windows.bat.
2. Set GCF_ENABLE_TRANSCRIPTION=true.
3. Prefer GCF_WHISPER_MODEL_PATH to an existing local model. Otherwise set GCF_WHISPER_ALLOW_MODEL_DOWNLOAD=true explicitly.
4. Restart backend; transcription may be queued after media analysis. Inference is local and may be CPU-intensive.

## Optional local vision

1. Install Ollama and a compatible vision model after reviewing license and hardware requirements.
2. Set GCF_ENABLE_VISION_ANALYSIS=true; default endpoint is http://127.0.0.1:11434/api/chat.
3. Restart backend and use Candidate Review → Analyze with vision, or let enabled media analysis queue assessments.
4. Scores are subjective and uncalibrated; sampled frames cannot reliably prove event timing. Candidates remain NEEDS_REVIEW.
5. Remote endpoints are blocked by default because frames may be private. Only set GCF_VISION_ALLOW_REMOTE=true after privacy review.

## Test and CI

From project root:

    python -m pip install -r backend\requirements.txt
    python -m pytest -q

Frontend:

    cd frontend
    npm install
    npm run build

Latest full code CI success: commit f6682f3 passed all five jobs in [GitHub Actions run 40356307571](https://github.com/zafuquu/GAMING-CLIP-FACTORY/actions/runs/40356307571). Ubuntu/Python 3.12 reported 62 passed, including synthetic FFmpeg rendering/QC; Windows/Linux Python 3.11/3.12 and the frontend build passed. This does not prove live provider credentials, source rights or the target machine.

## Important limitations

- No public discovery endpoint is treated as a reuse/download grant.
- Public upload/release gates are disabled by default.
- Global pause defers new queue claims and checks before outbound YouTube mutations, but it does not instantly kill an already-running FFmpeg process.
- No general user authentication is implemented; do not expose the API/dashboard to LAN/internet.
- Single local worker only. Multi-worker deployment, quota budgets, notifications, disk-pressure pause and extensive Windows stress testing remain future work.

## Documentation

- docs/ARCHITECTURE.md
- docs/AUTOMATION_ROADMAP.md
- docs/IMPLEMENTATION_STATUS.md
- docs/DEPENDENCY_AUDIT.md
- docs/ENVIRONMENT_AUDIT.md
- docs/SECURITY.md
