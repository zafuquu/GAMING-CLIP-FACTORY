# Architecture

## Local runtime

- React/TypeScript/Vite dashboard reads API data; SQLite is authoritative.
- FastAPI binds to 127.0.0.1:8765 by default.
- SQLite stores source rights, media assets, candidate moments, jobs, transcripts, content plans, caption segments, rendered outputs, publishing records, analytics snapshots, schedules/settings, discovery runs and system events.
- Numbered SQL migrations are applied incrementally.
- A single background worker consumes persistent jobs. FFmpeg/FFprobe subprocesses use argument lists, shell=False, captured output and finite timeouts.
- Restart recovery requeues interrupted local processing, but uncertain YouTube upload/publication is marked RECONCILE_REQUIRED instead of blindly retried.

## Source lifecycle

Provider discovery returns public metadata only. Twitch Helix and YouTube Data API integrations do not download videos or grant reuse rights. Source permission defaults to UNKNOWN. A source enters the production path only after license/permission evidence, editing and commercial-use flags, owner review and expiration checks pass. These checks repeat during import, analysis, review, render, upload and scheduled public release.

## Media lifecycle

1. Register authorized local media within the managed media root; record SHA-256.
2. FFprobe extracts technical metadata.
3. FFmpeg scene-change analysis creates candidate intervals marked NEEDS_REVIEW. Scene changes are heuristics rather than evidence of gameplay meaning.
4. Optional local Faster-Whisper produces speech transcript, timestamps and confidence when explicitly enabled.
5. Deterministic content planning uses source metadata and transcript evidence to propose a format/title/description/tags. It does not invent narration or assert unsupported events.
6. Owner approves or rejects a candidate.
7. Worker cuts the approved interval and places the full frame on a 1080×1920 fit-and-pad canvas. Caption timing can be saved to SRT and burned in if the FFmpeg subtitle filter is available.
8. FFprobe and an FFmpeg decode/defect scan create a QC report. Warnings produce REVIEW; only PASS is eligible for publishing.
9. Outputs are hashed, stored under the managed outputs directory and visible for browser preview.

## Provider and YouTube lifecycle

- Scheduled Twitch and YouTube discovery jobs upsert metadata, record discovery history and keep all source rights UNKNOWN.
- Optional YouTube public metadata search uses GCF_YOUTUBE_API_KEY; uploads require a separate OAuth Desktop flow and a connected channel.
- OAuth tokens are stored in the operating-system keyring.
- Uploads start private. An upload job requires a QC-passed output, matching hash/size and currently authorized permission. Uncertain upload outcome requires reconciliation.
- If public scheduling is separately enabled, the upload worker stages the video as private and queues a future worker job. At the scheduled time, the worker rechecks rights, QC, file hash and configuration before making the remote video public.
- Analytics is fetched via the official YouTube Analytics API and stored as timestamped snapshots; unavailable values are not fabricated.

## Not yet provided

Semantic visual event recognition, a learned highlight ranker, narration, thumbnail generation, Remotion compositions, extensive repair workflows, automatic source media acquisition, multi-worker scheduling and live credential/channel acceptance. No publisher treats a public URL as permission to reuse footage.
