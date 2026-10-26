# Automation Roadmap

This roadmap separates implemented code from owner configuration and live acceptance. Fully automated operation requires verified source rights, provider credentials and a connected YouTube channel. It must never bypass platform controls or permission requirements.

## Current alpha: implemented in code

- FastAPI and React dashboard, SQLite migrations 001–007, persistent worker jobs and restart recovery.
- Rights registry and repeat expiry/revocation checks; discovered sources remain UNKNOWN.
- Scheduled Twitch Helix and YouTube Data API metadata-only discovery; providers do not download footage.
- FFmpeg scene-change candidates, optional Faster-Whisper transcripts, timed captions and transcript-grounded draft metadata.
- Optional local Ollama vision assessment using up to three sampled frames; structured evidence and uncalibrated scores, with mandatory human review.
- Candidate review/approval, editable caption text/timing before approval, vertical rendering, SRT caption sidecar/burn-in, output registry and QC (PASS vs REVIEW).
- YouTube Desktop OAuth stored in keyring with scope-version validation, private upload, processing polling/recheck, reconciliation, Analytics API snapshots and a separate public-release environment gate.
- Opt-in recurring Analytics collection and same-window game/format comparison; a 'best supported group' is shown only when the group has at least three videos with returned average-view-percentage data.
- Public release starts from a private upload, rechecks current rights, output hash/size/QC and live remote processing state; owners can cancel or reschedule.
- Global pause checked at queue admission and immediately before outbound YouTube mutations. Queue transitions use immediate SQLite write locks, and one output can have only one publication record by database uniqueness.
- Current full CI passed on code commit f6682f3 (Windows/Linux Python 3.11/3.12, FFmpeg integration and frontend build): https://github.com/zafuquu/GAMING-CLIP-FACTORY/actions/runs/40356307571.

## Configuration required

- Twitch developer app credentials for Twitch metadata search.
- YouTube Data API v3 key for public metadata search and Google OAuth Desktop client for upload/processing/Analytics.
- Channel owner must authenticate. Scope-version mismatches require reconnecting with the local OAuth command.
- Valid, documented permission for each source, including intended editing and commercial reuse.
- Optional local Faster-Whisper model and optional Ollama vision model; no model downloads or inference happen unless configured.
- Target Windows FFmpeg/FFprobe acceptance and actual private-upload/reconciliation test using the intended YouTube channel.

## Remaining development phases

1. Reliability: keep current-head CI green, verify job recovery states, run target-Windows synthetic test including subtitles and stress-test pause behavior/in-flight operations.
2. Local production quality: transcript correction/review, better composition templates, safe areas, audio loudness/clipping checks, bounded repairs and quarantine.
3. Game-aware ranking: label rights-cleared examples per game; measure precision/recall and calibrate scores before relying on vision ranks. Three-frame VLM estimates can miss motion/context.
4. Content direction: optional evidence-grounded narration, thumbnails, template variety and owner-editable plans.
5. Publishing acceptance: real private upload, processing poll, uncertain outcome reconciliation, public scheduling/cancel/reschedule, permission expiry, output hash/QC mismatch and Analytics tests on the actual channel.
6. Supervised autonomy: quota budgets, alerting, disk-space pause, daily/storage limits, failure caps and restart testing before unattended operations.

## Public release gate

Public release is separately opt-in. Videos upload private first. Before making one public, the worker checks current authorization/expiry, policy gate, schedule, QC/hash/size and live YouTube processing/privacy status. Uncertain external outcomes require reconciliation rather than blind retries. If current policy no longer permits public visibility, reconciliation attempts to return the video to private.

## External prerequisites

- Authentic rights evidence for every source.
- Provider credentials/quotas and Google OAuth channel setup.
- Model and dependency license review for optional transcription/vision.
- Rights-cleared labeled clips and target-Windows acceptance.

## Reporting rule

IMPLEMENTED means code exists; TESTED means the current reproducible suite passed; NEEDS CONFIGURATION means owner setup/secrets are needed; NOT IMPLEMENTED or BLOCKED means a capability is absent or cannot be validated. Do not call the full factory production-verified until source discovery through authorized media, render/QC, private upload, processing/reconciliation and analytics pass end-to-end on the target environment.
