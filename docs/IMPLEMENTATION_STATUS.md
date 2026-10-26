# Implementation status

Release baseline: 0.2 alpha. The repository now contains a broader local production pipeline and credential-gated integrations, but it is **not yet fully automated end-to-end on a new machine**.

## Implemented in code

- FastAPI local API, SQLite persistence, numbered SQL migrations 001–004, and SQLite-backed job queue.
- Source registry with explicit permission evidence, editing/commercial-use permission, owner review and expiration. Sources discovered from providers remain UNKNOWN.
- Media-root containment, supported extensions, SHA-256 identity and duplicate file prevention.
- Twitch Helix metadata discovery behind Twitch app credentials. It finds clip metadata/URLs only and never downloads media.
- YouTube Data API public metadata search behind a local API key or stored OAuth credential. Search results also remain UNKNOWN for reuse rights.
- Configurable provider discovery schedules, job histories and duplicate queue prevention.
- FFprobe media metadata and FFmpeg scene-change candidate generation. Scene changes are heuristics, not semantic highlight detection.
- Optional local Faster-Whisper transcription. When explicitly enabled and installed, it stores transcript/segments, produces deterministic transcript-grounded content plan drafts and timestamped caption segments.
- Candidate approve/reject review, FFmpeg portrait fit-and-pad rendering at 1080×1920, optional subtitle burn-in/SRT sidecar, output SHA-256 and render registry.
- QC checks for nonempty output, probed dimensions/codecs/duration/audio, FFmpeg decode scan, black-frame and frozen-frame heuristics. Warning cases are REVIEW and are not eligible for upload.
- Local preview route for PASS and REVIEW output files stored under the managed outputs directory.
- Credential-gated YouTube OAuth via local browser flow and OS keyring; private resumable upload worker, history, uncertain-outcome reconciliation, analytics sync job and separately gated scheduled public-release worker.
- Public release worker rechecks current rights, output hash, size and QC immediately before changing YouTube privacy to public. Public publishing and uploads are disabled by default.
- Global pause, retry limits/backoff, job recovery after restart and a rule preventing blind retry of uncertain upload/publication outcomes.
- Dashboard sections for sources/rights, media, candidate review, queue, local output preview, provider discovery, transcription status, YouTube connection, publishing and analytics controls.
- CI workflow for Windows/Linux Python 3.11/3.12, Linux FFmpeg rendering integration, and frontend typecheck/production build.

## Verification evidence

- An earlier GitHub Actions run on commit 85f66fb2 passed the then-current backend matrix and frontend build; the Ubuntu/Python 3.12 job reported 11 tests passed including synthetic FFmpeg render/QC.
- Newer code has expanded CI coverage for provider settings, permission gates, transcript-grounded draft generation, review-state previews and rendering. The newest Actions run must finish before claiming those latest changes are green.
- The user's Windows PC has not yet passed a fresh end-to-end acceptance run against the exact FFmpeg package. Earlier version checks passed but their custom validator stopped while handling stderr; do not treat that as full local validation.
- No actual Twitch credentials, YouTube API key/OAuth connection, creator permission evidence, or real-channel upload have been validated from this repository.

## Not complete

- Reliable game-aware semantic vision analysis across multiple games and a calibrated highlight ranking model. Current candidates depend on scene-change heuristics; optional transcripts add context but do not establish the visual event.
- Caption correction/review, narration, thumbnail generation, diverse advanced templates, safe-area validation, and sophisticated automated repairs.
- Remotion integration (requires compatibility/licensing review).
- Automatic acquisition of source media. Current provider integrations return metadata and URLs only; the owner must provide properly authorized local media.
- Production hardening for high concurrency, richer quota-aware scheduling, notifications, authentication beyond local loopback, and longer-running Windows stress tests.
- Live provider API, YouTube OAuth/upload, analytics and real licensed-footage end-to-end verification.
- Autonomous public publishing is not enabled by default and must not be described as operational before the supervised acceptance suite passes.

Status vocabulary: IMPLEMENTED (code exists), TESTED (reproducible test passed), NEEDS CONFIGURATION (local owner credentials/configuration required), NOT IMPLEMENTED, BLOCKED. A committed file alone is not proof of working integration.
