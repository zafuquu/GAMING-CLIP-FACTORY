# Automation Roadmap

This roadmap translates the Gaming Clip Factory AI master specification into buildable, testable release gates. "Fully automated" means the complete pipeline can run from source discovery through publication and analytics when a valid, documented permission path and required credentials/configuration are present. It does not mean automatically overriding copyright, platform access, API quotas, account restrictions, or human review of ambiguous rights.

## Current release: 0.1 foundation

Implemented in `main`:
- Local FastAPI API and SQLite persistence.
- Manual source registration and rights evidence registry.
- Hard authorization gate for local media registration and queued analysis.
- Permission expiry is enforced at API and worker boundaries.
- Local media registration with media-root containment and SHA-256 fingerprinting.
- Persistent SQLite analysis job queue.
- FFprobe metadata and FFmpeg scene-change candidates with `NEEDS_REVIEW` state.
- Dashboard source, rights, media, queue, pause, and system-state views.
- Backend CI matrix for Windows/Linux and Python 3.11/3.12; dashboard build CI job added.

Known limits in this release:
- Source discovery is manual.
- Scene changes are heuristic candidates, not semantic game-aware highlight detection.
- Candidate approval and final clip rendering are not yet implemented.
- Automated caption/narration/content planning/thumbnail generation is not yet implemented.
- Full QC/repair and production output lifecycle are not yet implemented.
- Twitch or other provider integrations are not yet wired.
- YouTube OAuth, upload reconciliation, scheduling, and analytics are not implemented.
- No credentials have been configured; no public publishing is possible.

## Phase 1 — Harden the foundation

Exit criteria:
- CI green on all backend OS/Python matrix jobs and frontend typecheck/build.
- Rights expiry/revocation regression tests.
- Worker retry/backoff and stale-job recovery.
- Database connection lifecycle and migration tests.
- Structured log correlation IDs and health diagnostics.
- Verify FFmpeg/FFprobe version, codecs, synthetic media generation, segment render and FFprobe metadata on the target Windows PC.

Do not start public publishing.

## Phase 2 — Complete authorized local production

Build:
- Candidate review/approve/reject controls.
- Candidate-to-render job transition.
- Vertical video layouts that preserve source content (fit plus background treatment by default; crop only with explicit focus strategy).
- FFmpeg render process with finite timeout, unique outputs, cancellation state and persisted logs.
- Output registry, SHA-256, source lineage, template/config versions.
- Deterministic QC: codec, resolution, duration, audio presence, decodability, output size, black/frozen-frame heuristics and safe-area checks where feasible.
- Bounded repair attempts and quarantine.
- Human preview before publish.

Exit criteria: a rights-authorized local test file can go from import → analysis → reviewed candidate → rendered clip → QC result and persist through restart.

## Phase 3 — Content director and captions

Build:
- Transcription adapter with verified model/package licensing and offline option if practical.
- Caption timeline with alignment confidence, rendering, readability/safe-area checks and review edits.
- Content planning based only on transcript, source metadata and detected evidence.
- Configurable gameplay formats and non-repetitive template tracking.
- Deterministic baseline ranking with traceable score components; avoid learned models until labeled outcomes exist.

Exit criteria: reproducible render from a stored plan, with evidence explaining candidate and format choices.

## Phase 4 — Authorized source discovery and ingestion

Build a provider interface and only wire providers whose official API/terms permit the intended discovery and metadata operations. A public URL is never treated as reuse permission. Do not scrape or download around access controls.

For each provider, record API capabilities, scopes, quota, source URL/ID, rate limits and rights limitations. Where platform APIs do not provide downloadable media or reuse rights, the system must require user-supplied authorized media or a documented grant.

Exit criteria: scheduler discovers metadata, deduplicates it, stores rights state as UNKNOWN, and queues no unauthorized media for publication.

## Phase 5 — YouTube integration (credentials required)

Build OAuth through official Google APIs, scoped token persistence, secure local secret storage, connection/status UI, upload state machine, resumable uploads where supported, reconciliation after uncertain upload outcomes, privacy/scheduling, processing status, duplicate prevention, quota handling, and upload history.

Required human setup:
- Google Cloud project and enabled YouTube APIs.
- OAuth client configuration and consent requirements met.
- Owner authenticates the intended channel.
- Verify current YouTube API/account restrictions before enabling upload. Keep test uploads private/unlisted.
- Publishing always requires authorized source and QC-passed output.

Exit criteria: successful private test upload and restart-safe reconciliation; no public publishing yet.

## Phase 6 — Analytics and feedback

Build official YouTube Analytics API adapter, scheduled snapshots, missing/delayed-data semantics, comparable age windows (24h, 72h, 7d), experiments and reports. Never fabricate metrics. Strategy adjustments must be bounded and versioned; they cannot alter rights/security gates.

Exit criteria: stored analytics match API responses for a test channel, with failures/quota/permission limitations represented honestly.

## Phase 7 — Supervised automation

Build persisted schedule, worker supervision, queued/retrying job statuses, exponential backoff, per-provider quotas, storage limits, global pause, pause-on-critical-failure, notifications, startup recovery and a clear owner approval queue for rights/ambiguous decisions.

Exit criteria: authorized input goes through render/QC and reaches a scheduled private/unlisted publishing action with no duplicate uploads, proven with failure/restart tests.

## Phase 8 — Opt-in autonomous operation

Only after all prior gates pass:
- Owner explicitly enables autonomy.
- Start at low daily limits and private/unlisted or owner-review mode.
- Require all permissions valid/current.
- Require QC pass, unique idempotency keys, publishing limits, quota budget and no critical pause.
- Auto-pause on repeated failures, expired/revoked rights, credential/permission errors, quota risk, or low disk space.
- Public publication must be a separate explicit owner-enabled policy after the supervised acceptance suite passes.
- Keep an immediate global pause switch and reconciliation for non-idempotent external actions.

## Human/external prerequisites that cannot be invented in code

- Permission evidence or licenses covering editing and commercial reuse for each source.
- Provider/API credentials and scopes for chosen discovery sources.
- Google Cloud OAuth setup and channel owner authentication for YouTube.
- Current license review for FFmpeg build, Remotion, transcription/model weights, fonts, music and all templates/assets.
- An actual sample set of authorized gaming footage and human feedback for assessing whether highlights are useful.
- Target Windows machine validation for installed tool paths and rendering performance.

## Release reporting rule

Every feature is labeled one of: IMPLEMENTED, TESTED, NEEDS CONFIGURATION, NOT IMPLEMENTED, or BLOCKED. A committed file or passing unit test alone does not prove end-to-end production. Autonomous public publishing stays disabled until its explicit acceptance gates pass.
