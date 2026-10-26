# Automation Roadmap

This roadmap separates code that exists from owner configuration and live acceptance. Fully automated operation requires verified rights, provider credentials and a connected YouTube channel. It must never bypass platform controls or permission requirements.

## Current alpha: implemented in code

- FastAPI and React dashboard, SQLite migrations 001–009, persistent jobs and startup recovery.
- Rights registry and repeated expiry/revocation checks; discovered sources remain UNKNOWN.
- Scheduled Twitch Helix and YouTube Data API metadata-only discovery; providers do not download source footage.
- FFmpeg scene-change candidate generation, optional Faster-Whisper transcripts, timed captions and transcript-grounded draft metadata.
- Optional local Ollama-compatible vision assessment using up to three sampled frames; structured evidence/uncalibrated score, mandatory human review.
- Candidate review/approval, editable candidate-relative captions, vertical rendering with fit-and-pad or blurred-backdrop profile, SRT caption sidecar/burn-in and QC PASS/REVIEW.
- Generated portrait poster frames, hash/dimension verification and browser preview.
- YouTube Desktop OAuth in OS keyring with scope-version validation, private upload, processing polling/recheck, reconciliation, Analytics snapshots and separate public-release environment gate.
- Opt-in recurring Analytics collection and same-window game/format comparison; a group is highlighted only with at least three videos and an observed average-view-percentage metric.
- Public release begins from a private upload, rechecks current rights, output hash/size/QC, processing state and scheduled time; owners can cancel/reschedule.
- Global pause checks queue admission and outbound YouTube mutations. Queue transitions use immediate SQLite write locks; a partial unique index prevents multiple publication records for one render.
- Migration runner rejects duplicate version numbers and atomically commits each migration with its version marker; tests cover rollback/duplicate numbers.
- Latest hosted CI test steps passed on commit 63517f8f: Windows/Linux Python 3.11/3.12 and frontend build. Run 40358583264: https://github.com/zafuquu/GAMING-CLIP-FACTORY/actions/runs/40358583264.

## Configuration required

- Twitch developer app credentials for Twitch metadata search.
- YouTube Data API v3 key for public metadata search and Google OAuth Desktop client for upload/processing/Analytics.
- Channel owner must authenticate. Scope-version mismatch requires reconnecting with the local OAuth command.
- Valid documented permission for each source, including intended editing and commercial reuse.
- Optional local Faster-Whisper model and optional local Ollama vision model.
- Real private upload, processing poll, reconciliation and Analytics test using the intended YouTube channel.
- Windows target-machine validation with the exact local FFmpeg/FFprobe build and authorized/synthetic footage.

## Remaining development phases

### Phase 1 — reliability

- Verify worker/database failure transitions, migration rollback, duplicate remote operation prevention, and restart recovery under forced failures.
- Improve quota-aware provider retry classification, notification, low-disk pause, health diagnostics and Windows stress tests.
- Confirm the global pause semantics for active FFmpeg subprocesses and in-flight YouTube HTTP operations.

### Phase 2 — quality rendering

- Full caption editing/review plus transcript correction and subtitle safe-area/readability checks.
- More render profiles and optional region-of-interest layouts, with no forced crop unless chosen by a documented focus strategy.
- Loudness/clipping checks, missing-stream checks, artifact size/duration policies, quarantine and bounded repair.
- Custom thumbnail/layout assets and automated visual safe-area tests.

### Phase 3 — reliable gameplay event understanding

Scene-change detection and three sampled vision frames are not reliable enough to drive unattended selection. Add optional vision sampling over time, motion/audio cues, game-specific HUD understanding where supported, near-duplicate detection, explainable evidence and a calibrated scoring pipeline trained/evaluated on labeled rights-cleared footage. Low confidence must remain review-only; do not invent events.

### Phase 4 — content direction/presentation

- Transcript correction, evidence-grounded hook/title/description/tags editing.
- Optional narration only from verified facts/owner script.
- Template variants (clutch, action, reaction, educational, general gameplay), versioning and thumbnail generation.
- Remotion integration only after current compatibility and licensing review.

### Phase 5 — authorized acquisition and intake

The existing provider integrations return metadata, not files. Do not add generic scrapers/downloaders that bypass access controls or terms. Add media acquisition only where official API/license/owner grants clearly permit the intended operation. New sources remain UNKNOWN until rights are verified.

### Phase 6 — YouTube supervised acceptance

- Validate private test upload, remote processing lifecycle, recheck and manual reconciliation after forced interruption.
- Test expired/revoked rights before public release, QC/hash mismatch, global pause, reschedule/cancel and API errors.
- Verify actual API quotas, channel/account policy and current OAuth scopes.
- Keep public release disabled until the supervised acceptance suite passes.

### Phase 7 — analytics/optimization

- Scheduled Analytics pulls with quotas and timestamps.
- Comparable-age reports, missing/delayed-value semantics and analysis by game/format/duration/hook/posting time.
- Controlled experiments only with adequate sample size. Optimizers must never alter rights, security, QC, secret or pause gates.

### Phase 8 — supervised then autonomous

Supervised acceptance must prove that rights-authorized input can reach a local QC-passed clip, a private upload, successful processing, reconciliation and Analytics without duplicates or unsafe retry. Only then consider owner-enabled autonomy, low daily limits, private/unlisted operation first, automatic pause on critical errors, and a separate explicit public-release gate.

## External prerequisites

Permission evidence, Twitch/Google credentials, channel-owner OAuth, model weights/licenses if enabled, rights-cleared sample footage and target Windows acceptance cannot be fabricated in code.

## Reporting rule

Every feature must be labeled IMPLEMENTED, TESTED, NEEDS CONFIGURATION, NOT IMPLEMENTED or BLOCKED. Code presence does not prove live integration. Public publishing remains disabled by default.
