# Architecture

## Local runtime

- React/TypeScript/Vite dashboard reads API data; SQLite is authoritative.
- FastAPI binds to 127.0.0.1:8765 by default.
- SQLite stores source rights, media assets, candidates, jobs, transcripts, content plans, captions, rendered outputs/posters, publishing records, Analytics snapshots, settings/schedules, discovery runs and system events.
- Numbered migrations 001–012 are checked for unique version numbers and applied atomically with the schema-version marker. Migration tests cover duplicate numbering and rollback of a broken migration.
- A single worker consumes persistent jobs. Critical queue admission/publication transitions use immediate SQLite write locks. FFmpeg subprocesses use argument arrays, shell=False and finite timeouts.
- Restart recovery requeues interrupted local processing but marks uncertain external YouTube operations RECONCILE_REQUIRED instead of blindly retrying.

## Source lifecycle

Twitch Helix and YouTube Data API return public metadata only; they do not download videos or grant reuse rights. New source permissions default to UNKNOWN. Source media enters production only after permission/license evidence, editing and commercial-use flags, owner review and expiry checks pass. These checks repeat during intake, analysis, candidate decision, render, upload and public release.

## Media lifecycle

1. Register authorized local media under the managed media root and record SHA-256.
2. FFprobe extracts technical metadata.
3. FFmpeg scene-change analysis creates candidate intervals marked NEEDS_REVIEW. Scene changes are heuristics, not evidence of gameplay meaning.
4. Optional Faster-Whisper creates local speech transcripts and timestamps.
5. Deterministic content planning uses source metadata/transcript evidence and does not invent narration or unsupported gameplay events.
6. Optional local vision analysis samples three frames and stores structured visible-event/evidence estimates and an uncalibrated score. It never auto-approves.
7. Owner edits caption timings if needed and approves/rejects the candidate.
8. Worker creates either a fit-and-pad 1080×1920 render or a blur-fill composition that places the full uncropped foreground frame over a blurred background. Timed captions may be burned in and SRT is retained when available.
9. FFprobe/FFmpeg defect scans create QC. Black/frozen intervals or missing subtitle filters produce REVIEW; only PASS qualifies for publishing.
10. A 720×1280 portrait poster frame is extracted from the final video, probed, hashed and stored locally. MP4/poster are previewable in the dashboard.
11. Rendered output records profile, lineage, SHA-256 and a QC report. One rendered output can have only one local publication record.

## Provider and YouTube lifecycle

- Scheduled Twitch/YouTube metadata discovery deduplicates results and keeps source rights UNKNOWN.
- YouTube OAuth Desktop flow stores tokens in the OS keyring. A scope-version marker requires owner reconnect if scopes change.
- Uploads begin private. An upload job requires a QC-passed output, matching hash/size and current authorized permission.
- YouTube processing status is polled before a scheduled release can proceed. Uncertain upload/privacy outcomes require reconciliation.
- If public scheduling is separately enabled, the worker checks release time, current permission, output hash/size/QC, upload gate and live YouTube processing/privacy status immediately before making the remote video public. Owner can cancel/reschedule.
- Analytics is fetched through official YouTube Analytics API and stored as timestamped snapshots. Game/format comparison uses exact same-date windows and requires at least three videos before highlighting a cohort; small or absent samples do not create fake results.

## Not yet provided

Reliable calibrated game-aware visual event recognition, narration, richer composition/template system, safe-area checks, automatic source-media acquisition, multi-worker scheduling, extensive repair workflows, and live credential/channel acceptance. Custom cover generation/upload code exists but live account eligibility has not been verified. Public URLs never imply permission to reuse footage.
