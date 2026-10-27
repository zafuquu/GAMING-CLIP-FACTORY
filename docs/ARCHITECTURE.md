# Architecture

## Local runtime

- React/TypeScript/Vite dashboard reads API data; SQLite is authoritative.
- FastAPI binds to 127.0.0.1:8765 by default.
- SQLite stores source rights, media assets, candidates, jobs, transcripts, content plans, captions, rendered outputs/posters, publishing records, Analytics snapshots, settings/schedules, discovery runs and system events.
- Numbered migrations 001–020 are checked for unique version numbers and applied atomically with the schema-version marker. Migration tests cover duplicate numbering and rollback of a broken migration.
- A single worker consumes persistent jobs. Persisted discovery and Analytics schedules are checked every worker cycle before queue claims, so a busy job queue cannot starve scheduled runs; scheduler faults are logged without blocking local work. Critical queue admission/publication transitions use immediate SQLite write locks. FFmpeg subprocesses use argument arrays, shell=False and finite timeouts.
- Restart recovery requeues interrupted local processing but marks uncertain external YouTube operations RECONCILE_REQUIRED instead of blindly retrying.

## Source lifecycle

Twitch Helix and YouTube Data API return public metadata only; they do not download videos or grant reuse rights. New source permissions default to UNKNOWN. Source media enters production only after permission/license evidence, editing and commercial-use flags, owner review and expiry checks pass. These checks repeat during intake, analysis, candidate decision, render, upload and public release.

## Media lifecycle

1. Register authorized local media under the managed media root and record SHA-256.
2. FFprobe extracts technical metadata.
3. FFmpeg scene-change analysis creates candidate intervals marked NEEDS_REVIEW. Scene changes are heuristics, not evidence of gameplay meaning.
4. Optional Faster-Whisper creates local speech transcripts and timestamps. Re-transcription rechecks rights and file identity after the subprocess; existing content plans and captions marked as owner-edited are not overwritten.
5. Deterministic content planning uses source metadata/transcript evidence and does not invent narration or unsupported gameplay events.
6. Optional local vision analysis samples 3 or 5 frames (default 5), passes candidate-relative timestamps with the images, and stores structured visible-event/evidence estimates, source timestamps and relative offsets. Scores are uncalibrated and it never auto-approves.
7. By default, the owner edits captions and approves/rejects each candidate. For an explicitly opted-in authorized folder watch, supervised auto-production may auto-approve and render at most three scene-change candidates per media file after checking the watch, pause state and current source rights. This is a convenience batch, not semantic highlight approval: scene changes are uncalibrated, and the finished local clips still require owner quality review.
8. Worker creates either a fit-and-pad 1080×1920 render or a blur-fill composition that places the full uncropped foreground frame over a blurred background. Timed captions may be burned in and SRT is retained when available.
9. FFprobe/FFmpeg defect scans create QC. Black/frozen intervals or missing subtitle filters produce REVIEW; only PASS qualifies for publishing.
10. A 720×1280 portrait poster frame is extracted from the final video, probed, hashed and stored locally. MP4/poster are previewable in the dashboard.
11. Rendered output records profile, lineage, SHA-256 and a QC report. One rendered output can have only one local publication record.
12. The supervised auto-production option only queues local render jobs. It never enables YouTube uploads or schedules public release; upload/publication policy remains controlled by its independent gates.

## Provider and YouTube lifecycle

- Scheduled Twitch/YouTube metadata discovery deduplicates results and keeps source rights UNKNOWN.
- YouTube OAuth Desktop flow stores tokens in the OS keyring. A scope-version marker requires owner reconnect if scopes change.
- Uploads begin private. An upload job requires a QC-passed output, matching hash/size and current authorized permission.
- YouTube processing status is polled before a scheduled release can proceed. Uncertain upload/privacy outcomes require reconciliation.
- If public scheduling is separately enabled, the worker checks release time, current permission, output hash/size/QC, upload gate and live YouTube processing/privacy status immediately before making the remote video public. Owner can cancel/reschedule.
- Analytics is fetched through official YouTube Analytics API and stored as timestamped snapshots. Game/format comparison uses exact same-date windows and requires at least three videos before highlighting a cohort; small or absent samples do not create fake results.

## Not yet provided

Reliable calibrated game-aware visual event recognition, high-quality story/narration direction and expression controls, richer composition/template system, safe-area checks, automatic source-media acquisition, multi-worker scheduling, extensive repair workflows, and live credential/channel acceptance. Custom cover generation/upload code exists but live account eligibility has not been verified. Public URLs never imply permission to reuse footage.
