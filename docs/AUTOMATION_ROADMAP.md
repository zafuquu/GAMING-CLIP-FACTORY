# Automation Roadmap

This roadmap states the actual capability boundaries and remaining work for Gaming Clip Factory AI. Fully automated operation means the pipeline can run with owner-approved sources, valid provider credentials and a connected channel. It never means bypassing copyright, provider terms, API quotas, authentication, or review of ambiguous permissions.

## Current release: local production and integration alpha

### Implemented in code

- Local FastAPI API, SQLite persistence, numbered migrations and background job queue.
- Rights registry with evidence, editing/commercial-use flags, owner review and expiry checks.
- Safe local media registration with hashing and duplicate prevention.
- Twitch Helix metadata discovery with scheduled searches; YouTube Data API public metadata search with a local API key or connected OAuth. Results always remain UNKNOWN for reuse rights and no source media is downloaded.
- FFprobe metadata and FFmpeg scene-change candidate windows, marked for human review.
- Optional local Faster-Whisper transcript/phrase timestamps; deterministic transcript-grounded draft plans and timed caption segments.
- Candidate approve/reject UI, FFmpeg portrait cut/layout, optional caption burn-in/SRT sidecar, output registry and hashes.
- QC checks for probe/decode, dimensions, duration, codecs/audio, black-frame/frozen-frame heuristics. REVIEW results are visible for preview but not publishable; only PASS is eligible for upload.
- Private YouTube resumable upload adapter, OAuth stored in OS keyring, upload history and uncertain-outcome reconciliation; Analytics API snapshots are supported.
- A separately gated public-release worker stages uploads as private then checks rights, output hash and QC immediately before making a video public at its scheduled time.
- Global pause, bounded subprocess timeouts, retry/backoff, interrupted-job recovery and a dashboard for status/control.
- CI covering Windows/Linux Python 3.11/3.12, FFmpeg render integration on Linux and frontend build.

### Configuration required

- Twitch: create an app in the Twitch developer console; set GCF_TWITCH_CLIENT_ID and GCF_TWITCH_CLIENT_SECRET in local .env.
- YouTube metadata search: set GCF_YOUTUBE_API_KEY in local .env or connect OAuth.
- YouTube channel upload/analytics: create OAuth Desktop Client in Google Cloud Console, set GCF_YOUTUBE_CLIENT_SECRETS to a file path outside repo, run python -m app.youtube_auth from backend, then test the real channel. Uploads require GCF_ENABLE_YOUTUBE_UPLOADS=true and an explicit dashboard toggle.
- Public scheduled release is a separate high-risk gate: GCF_ALLOW_SCHEDULED_PUBLIC_PUBLISHING=true, only after private upload tests and end-to-end rights/QC acceptance.
- Optional transcription: install scripts/setup-ml-windows.bat; set GCF_ENABLE_TRANSCRIPTION=true; provide a local model path or explicitly allow model download.
- Every source: provide/document actual rights that allow editing and commercial use; provider metadata does not grant those rights.
- Windows: verify the user's exact FFmpeg/FFprobe build and local end-to-end rendering (the previous manual validator failed while processing stderr).

## Phase 1 — Reliability and regression hardening

Exit criteria:
- Latest CI run green on all Windows/Linux backend jobs, frontend build and integration tests.
- Add integration tests for upload reconciliation, schedule rights expiry, QC review/preview, transcription persistence and public-release cancellation.
- Verify all database connection/transaction paths, job state transitions and startup-recovery cases.
- Correct quota-aware retry policy and record API failure categories without logging credentials.
- Run the real Windows FFmpeg acceptance suite using authorized synthetic media, including subtitle rendering and QC review paths.
- Confirm global pause prevents new work; document behavior for already-running FFmpeg and in-flight HTTP calls.

No unattended public publishing in this phase.

## Phase 2 — Stronger local production

Build:
- Testable render profiles and output/template versioning.
- Better vertical composition: optional blurred-background fill, crop/region-of-interest options and visual safe areas.
- Candidate/caption editing UI, transcript correction, preview and render retry controls.
- More robust QC: missing streams, aspect ratio, loudness/clipping, long black intervals, frame freeze, decode errors, caption timing/overlap/safe areas, output size and duration.
- Quarantine, bounded repair attempts and render cancellation/restart recovery.
- Automated tests for a matrix of landscape/portrait input, audio/no audio, short/long video, corrupt input and subtitle filter missing.

## Phase 3 — Genuine game-aware highlight analysis

Scene-change detection alone is not enough. Implement providers through explicit model adapters:
- sample frames around candidate intervals
- optional audio/speech signals
- game-specific visual indicators where supported
- candidate evidence and confidence
- ranking component traceability
- duplicate / near-duplicate clip detection
- minimum context and dead-time controls

First verify the intended vision model's Windows/GPU compatibility, license, memory needs and evaluation quality. Do not invent events. Maintain deterministic fallback and human-review status for low confidence. Train/rank learned models only after collecting labeled authorized examples.

## Phase 4 — Better content direction and presentation

Build:
- Transcript/caption correction and confidence display.
- Evidence-grounded hook, title, description and tags with review/edit fields.
- Optional narration only from verified facts or owner-provided script.
- Templates for clutch, funny moment, action, explanation and general gameplay.
- Thumbnail generation using available frames/approved assets.
- Template/style variety and strategy versions.
- Remotion integration only after version, Windows compatibility and licensing review.
- Render profiles and test assets with rights-cleared fonts/music/sound effects.

## Phase 5 — Source acquisition and rights-aware intake

The existing Twitch and YouTube integrations discover metadata, not files. Do not add generic scraping/downloaders that bypass platform access or terms.
- Add providers only if official API/license/owner grant permits intended operations.
- Where download/media access is not supported, require owner-supplied authorized files or a documented creator grant.
- Explore a separate permission/grant registry for creator contracts, licenses, attribution and revocation.
- If a valid rights document can be mechanically verified, build a provider-specific verifier; it must fail closed for ambiguity.
- Use scheduled queries, deduplication, backoff, quota accounting and source-provider health.
- Keep all new sources UNKNOWN until rights are verified.

## Phase 6 — YouTube publishing acceptance

Before any public release:
- Configure Google Cloud and OAuth, authenticate intended channel, confirm necessary scopes and current API quota/account restrictions.
- Validate real private upload and processing status on a test video.
- Validate duplicate prevention and manual reconciliation after forced network interruption.
- Test scheduled job restart, permission expiry/revocation before release, file hash mismatch, QC downgrade, global pause and API errors.
- Confirm YouTube API processing status and upload-history persistence.
- Build a cancel/reschedule control for locally scheduled public-release jobs.
- Keep public release disabled until the supervised suite is green and owner explicitly enables the environment policy.

## Phase 7 — Analytics and optimization

- Schedule analytics collection with quotas/rate limits.
- Store snapshots and source API retrieval timestamps.
- Add comparable-age views (24h, 72h, 7d) and performance by game, format, duration, hook and publication time.
- Report missing/delayed fields as missing, not zero.
- Introduce controlled experiments with minimum sample sizes and confidence signals.
- Keep strategy changes versioned and bounded. The optimizer can never modify rights rules, QC gates, secrets, global pause or upload/publication limits.

## Phase 8 — Supervised then autonomous operation

Supervised exit criteria:
- Authorized input flows through metadata/analysis, candidate review, render/QC, private upload, reconciliation and analytics without duplicate operations.
- Startup recovery and in-flight external operations have demonstrated tests.
- Repeated failures, quota/auth errors, low disk, expired rights or QC regressions trigger a pause/alert.
- All source permissions are current and auditable.
- Owner can stop queueing immediately and inspect every produced output.

Autonomous mode should be separately enabled by owner after supervised acceptance:
- begin with low daily and storage limits
- private/unlisted publication until explicitly cleared
- always recheck current rights, QC and output hash
- use idempotency and reconcile non-idempotent outcomes
- automatically pause on critical failures and never auto-bypass rights, API or security gates
- keep public publishing behind the separate environment gate

## External prerequisites

- Legal permission evidence for every source.
- Provider credentials and quotas for chosen metadata sources.
- Google Cloud OAuth client and user authentication for YouTube.
- Model weights and license review if local transcription/vision is enabled.
- Real, rights-cleared sample footage for quality evaluation.
- Target Windows PC validation.

## Reporting rule

Every feature is labeled IMPLEMENTED, TESTED, NEEDS CONFIGURATION, NOT IMPLEMENTED or BLOCKED. Code presence or a passing unit test does not prove the end-to-end service works. Do not claim full automation until the whole pipeline passes on the target Windows environment with a real authenticated test channel and rights-cleared footage.
