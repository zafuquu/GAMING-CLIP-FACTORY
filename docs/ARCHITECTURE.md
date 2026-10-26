# Architecture

## Local runtime

- React/TypeScript/Vite dashboard reads API data; SQLite is authoritative.
- FastAPI binds to 127.0.0.1:8765 by default.
- SQLite stores source rights, media assets, candidates, jobs, transcripts, content plans, captions, rendered outputs, publishing records, Analytics snapshots, settings, schedules, discovery runs and system events.
- Incremental numbered migrations 001–007 include a partial unique index so a render cannot have multiple publication records.
- A single worker consumes persistent jobs. Critical queue-admission/publication transitions use immediate SQLite write locks; FFmpeg subprocesses use argument arrays, shell=False and finite timeouts.
- Restart recovery requeues local processing but marks uncertain external YouTube operations RECONCILE_REQUIRED instead of blindly retrying.

## Source lifecycle

Twitch Helix and YouTube Data API return public metadata only; they do not download videos or grant reuse rights. New source permissions default to UNKNOWN. Source media enters production only after license/grant evidence, editing and commercial-use flags, owner review and expiration checks pass. These checks repeat during intake, analysis, candidate decision, render, upload and public release.

## Media lifecycle

1. Register authorized local media under the managed media root and record SHA-256.
2. FFprobe extracts technical metadata.
3. FFmpeg scene-change analysis creates candidate intervals marked NEEDS_REVIEW. Scene changes are heuristics, not evidence of gameplay meaning.
4. Optional Faster-Whisper creates local speech transcripts and timestamps.
5. Deterministic content planning uses source metadata/transcript evidence; it does not invent narration or assert unsupported gameplay events.
6. Optional local vision analysis samples three frames and stores structured visible-event/evidence estimates and an uncalibrated score. It never auto-approves.
7. Owner approves/rejects candidate.
8. Worker creates fit-and-pad 1080x1920 render, optionally burns timed captions and persists SRT when available.
9. FFprobe/FFmpeg defect scans create QC: warnings produce REVIEW, only PASS qualifies for publishing.
10. Outputs are hashed, stored and previewable in the dashboard.

## Provider and YouTube lifecycle

- Scheduled Twitch/YouTube metadata discovery deduplicates results and keeps rights UNKNOWN.
- YouTube OAuth Desktop flow stores tokens in OS keyring. A scope-version marker requires the owner to reconnect if configured scopes change.
- Uploads start private and require QC PASS, current rights and matching output hash/size. YouTube processing status is polled before the result is treated as ready; timeouts can be explicitly rechecked.
- If public scheduling is separately enabled, the worker rechecks source rights, expiration relative to schedule, output QC/hash, release time, configuration and live remote processing/privacy before changing remote state.
- Public release can be canceled/rescheduled; unexpected public state on reconciliation is forced private unless current policy/rights/output state support it.
- Analytics comes from the official API and is stored as timestamped snapshots; missing values are not fabricated.

## Not yet provided

Calibrated semantic gameplay-event recognition, verified game-specific ranking, AI narration, thumbnails, Remotion compositions, robust template diversity, automatic media acquisition, multi-worker scheduling and live credential/channel acceptance remain future work. Public URLs never imply permission to reuse footage.
