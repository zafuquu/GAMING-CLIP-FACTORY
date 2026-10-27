# Implementation status

Release baseline: 0.3 development alpha. The repository contains a substantial local-first production pipeline, but it is not fully hands-off end-to-end on a new machine.

## Implemented in code

- FastAPI local API, React dashboard, SQLite persistence, numbered migrations 001–012, persistent job queue and startup recovery. Migrations reject duplicate version prefixes and apply each schema file plus its version marker atomically.
- Source rights registry with evidence, editing/commercial-use flags, owner review and expiry checks. Discovered sources remain UNKNOWN.
- Local media registration restricted to the configured media root with file checks, SHA-256 and duplicate prevention.
- Twitch Helix and YouTube Data API metadata-only discovery with schedules/history. No source media is downloaded and public metadata never grants reuse rights.
- FFprobe metadata and FFmpeg scene-change candidates; still a heuristic detector rather than semantic game-event understanding.
- Optional Faster-Whisper transcription, timed phrase segments, deterministic transcript-grounded content drafts and caption timing.
- Optional local Ollama-compatible vision assessment sampling three frames, saving structured evidence and an uncalibrated score. Local inference is preferred, feature is off by default and never auto-approves.
- Candidate review/approval, editable candidate-relative captions, H.264/AAC 1080×1920 fit-and-pad or blurred-backdrop rendering, optional SRT subtitle burn-in and output hashes.
- Local portrait poster frame extraction at 720×1280, poster dimensions/hash and browser preview; deterministic title-led Shorts cover generation from the real rendered frame, artifact hash/dimension validation, official YouTube thumbnail upload after processing, owner retry/regeneration and separate thumbnail-specific status/error tracking.
- QC checks for probe/decode, dimensions, duration, codecs/audio, black-frame and frozen-frame heuristics. Warnings become REVIEW; only PASS is eligible for YouTube.
- Browser preview for PASS and REVIEW outputs, transcript/content-plan/vision evidence exposed in API/dashboard.
- YouTube OAuth Desktop stored in OS keyring with scope-version checking; private resumable upload; processing-status polling/manual recheck; official custom Shorts cover generation/upload with a separate retryable state machine; Analytics API snapshots and schedule; same-window game/format report with a three-video minimum sample; upload reconciliation; separately gated scheduled-public-release worker.
- Public-state reconciliation validates channel ownership, current rights, output QC/hash, schedule and remote processing state, and attempts to make unexpected public videos private when policy no longer permits publication.
- Owner cancel/reschedule controls, global-pause checks before outbound YouTube operations, immediate SQLite write locks and a partial unique index allowing only one publication record per render.
- Bounded subprocess timeouts/retries, job recovery, migration collision/rollback tests and current CI.

## Latest verification evidence

GitHub Actions run 40358583264 on commit 63517f8f ran all five jobs. The four backend test steps passed: Windows/Python 3.11 and 3.12 each reported 70 passed, 2 skipped; Ubuntu/Python 3.11 and 3.12 each reported 72 passed including FFmpeg render integration. The frontend typecheck/production build passed. At time of inspection, workflow cleanup was still in progress. Link: https://github.com/zafuquu/GAMING-CLIP-FACTORY/actions/runs/40358583264.

An earlier completed CI run 40356307571 also passed the then-current full matrix. Hosted CI does not verify the user's local Windows FFmpeg package, live provider credentials, source licenses or a real YouTube upload.

## Configuration and acceptance still required

- Twitch developer-app credentials and/or YouTube Data API key.
- Google Cloud OAuth Desktop client and owner authentication for the intended channel. Scope-version mismatches require reconnecting with the local OAuth command.
- Legal permission evidence for each source allowing intended editing and commercial reuse.
- Optional speech model package/model files and optional local Ollama vision model; each feature is opt-in.
- Real private upload, YouTube processing poll, reconciliation and Analytics verification with the intended channel.
- End-to-end Windows acceptance on the target machine using authorized or synthetic footage.

## Not complete

- Reliably calibrated game-aware highlight classification across games. Scene-change detection and sparse-frame vision models can miss motion/context; scores are not calibrated.
- Full transcript correction workflow, narration, owner-editable design templates, richer compositions, safe-area validation and sophisticated repair/quarantine. Designed cover code exists, but real channel eligibility for custom thumbnail upload remains unverified.
- Automatic source-media acquisition; providers currently search metadata only.
- Production hardening for multiple workers, quota budgeting, authentication beyond loopback, notifications, low-disk pause and Windows stress tests.
- Unattended autonomous publishing. Public release is disabled by default and must not be described as production-verified before supervised acceptance passes.

Code presence does not prove a live integration works. Continue labeling capabilities IMPLEMENTED, TESTED, NEEDS CONFIGURATION, NOT IMPLEMENTED or BLOCKED.
