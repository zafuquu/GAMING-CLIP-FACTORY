# Implementation status

Release baseline: 0.3 development alpha. The repository contains a substantial local-first production pipeline, but it is not fully hands-off end-to-end on a new machine.

## Implemented in code

- FastAPI local API, React dashboard, SQLite persistence, numbered migrations 001–020, persistent job queue and startup recovery. Migrations reject duplicate version prefixes and apply each schema file plus its version marker atomically.
- One-click Windows startup through `scripts/start.bat`: checks runtime and dependency health, repairs missing environment dependencies, builds the optimized frontend, starts/reuses the API and opens a same-origin local dashboard. `/api/readiness` reports database, FFmpeg/FFprobe, writable paths and worker liveness. The Windows launch path still requires acceptance on the actual target PC.
- Source rights registry with evidence, editing/commercial-use flags, owner review and expiry checks. Discovered sources remain UNKNOWN.
- Local media registration restricted to the configured media root with file checks, SHA-256 and duplicate prevention.
- Twitch Helix and YouTube Data API metadata-only discovery with schedules/history. No source media is downloaded and public metadata never grants reuse rights.
- FFprobe metadata plus a local FFmpeg ranker combining relative audio peaks, scene-change timing, and low-resolution frame-difference motion sampled twice per second. This requires no external API or downloaded AI model, but remains an uncalibrated heuristic rather than semantic game-event understanding.
- Optional Faster-Whisper transcription, timed phrase segments, deterministic transcript-grounded content drafts and caption timing. A post-subprocess rights/file-identity recheck prevents persistence after revocation or source mutation; owner-edited plans and manually corrected captions survive re-transcription.
- Optional OpenRouter multimodal highlight judgment batches up to four signal-ranked candidates per request, samples three event-centered frames per candidate, records the selected free model, and saves structured evidence/scores. Full video scanning and rendering remain local; sampled frames and prompt context go to OpenRouter. Auto-render is queued only when the semantic gate passes; public publishing is not part of this path.
- Candidate review/approval, editable candidate-relative captions, H.264/AAC 1080×1920 fit-and-pad or blurred-backdrop rendering, optional SRT subtitle burn-in and output hashes.
- Opt-in supervised folder auto-production: stable files can be imported/analyzed, and up to three candidates per media file can be automatically rendered locally after current rights/watch/pause checks. The lifetime cap is stored on candidate records so repeated analysis cannot create extra batches. Unselected candidates remain in manual review; rendered outputs still need human inspection.
- Separate opt-in `auto_upload_private` folder-watch setting (migration 019, merged in `3f3a451da9846a1f1085567078d84acfe0d32d56`): requires current rights, connected YouTube OAuth and both environment and dashboard/database upload gates. After a QC-PASS render, rights and source identity are rechecked before the output and one private-upload job are recorded atomically. The worker checks current watch consent, rights, pause and rendered-output hash again before the provider request. It queues at most one private upload per source recording and does not schedule public release.
- Local portrait poster frame extraction at 720×1280, poster dimensions/hash and browser preview; deterministic title-led Shorts cover generation from the real rendered frame, artifact hash/dimension validation, official YouTube thumbnail upload after processing, owner retry/regeneration and separate thumbnail-specific status/error tracking.
- QC checks for probe/decode, dimensions, duration, codecs/audio, black-frame and frozen-frame heuristics. Warnings become REVIEW; only PASS is eligible for YouTube.
- Browser preview for PASS and REVIEW outputs, transcript/content-plan/vision evidence exposed in API/dashboard.
- YouTube OAuth Desktop stored in OS keyring with scope-version checking; private resumable upload; processing-status polling/manual recheck; official custom Shorts cover generation/upload with a separate retryable state machine; Analytics API snapshots and schedule; same-window game/format report with a three-video minimum sample; upload reconciliation; separately gated scheduled-public-release worker.
- Public-state reconciliation validates channel ownership, current rights, output QC/hash, schedule and remote processing state, and attempts to make unexpected public videos private when policy no longer permits publication.
- Scheduled public release verifies YouTube reports `privacyStatus=public` before recording `PUBLISHED`; if the update response omits status, the worker performs an authoritative read. A video still reported private is not recorded as published; ambiguous remote state requires reconciliation.
- Owner cancel/reschedule controls, global-pause checks before outbound YouTube operations, immediate SQLite write locks and a partial unique index allowing only one publication record per render.
- Bounded subprocess timeouts/retries, job recovery, migration collision/rollback tests and current CI.

## Latest verification evidence
Verification history:

- The dependency-lock/installer change passed all six jobs on exact PR head `9e8e8c172670dd6d5920c472ab20f1c20e37f51c` ([run #411](https://github.com/zafuquu/GAMING-CLIP-FACTORY/actions/runs/40485109430)) and is merged to `main` as `2946cba2ef683c61f0b61678c8fd83c97b349b3d`.
- The opt-in private-upload change passed all six jobs on exact PR head `e743464e1c44bfb943d6e4ce53fe2323ec3b21b7` ([run #413](https://github.com/zafuquu/GAMING-CLIP-FACTORY/actions/runs/40486137865)) and is merged as `3f3a451da9846a1f1085567078d84acfe0d32d56`. Its synthetic test checks one private upload, no public-release job, gate combinations, cancellation after watch opt-out, and hash validation.
- Render-artifact cleanup passed all six jobs on exact PR head `b88b627e9b571cf0b68bb33aab0a657507b25c11` ([run #415](https://github.com/zafuquu/GAMING-CLIP-FACTORY/actions/runs/40489491330)) and is merged as `357b5632c0e5232cb9afd0b31e5d54ada2f7df41`.
- The CI run triggered by the latest `main` commit must be checked for the final status; a green PR run and a green previous commit do not substitute for post-merge verification.
## Configuration and acceptance still required

- Twitch developer-app credentials and/or YouTube Data API key.
- Google Cloud OAuth Desktop client and owner authentication for the intended channel. Scope-version mismatches require reconnecting with the local OAuth command.
- Legal permission evidence for each source allowing intended editing and commercial reuse.
- Optional speech model package/model files and optional local Ollama vision model; each feature is opt-in.
- Real private upload, YouTube processing poll, reconciliation and Analytics verification with the intended channel.
- End-to-end Windows acceptance on the target machine using authorized or synthetic footage.

## Not complete

- Reliably calibrated game-aware highlight classification across games. The motion/audio/scene ranker improves local signal coverage but frame differences cannot distinguish camera motion from genuine action; sparse-frame vision can also miss temporal context. Scores are not calibrated.
- Full transcript correction workflow, higher-quality narration direction/pronunciation/emotion controls, owner-editable design templates, richer compositions, safe-area validation and sophisticated repair/quarantine. Local Piper script-to-WAV synthesis and FFmpeg render mixing are implemented but require a suitable local voice, licensing review and listening/quality validation. Designed cover code exists, but real channel eligibility for custom thumbnail upload remains unverified.
- Automatic source-media acquisition; providers currently search metadata only.
- Production hardening for multiple workers, quota budgeting, authentication beyond loopback, notifications, storage-quota budgets and Windows stress tests.
- Unattended autonomous publishing. Public release is disabled by default and must not be described as production-verified before supervised acceptance passes.

Code presence does not prove a live integration works. Continue labeling capabilities IMPLEMENTED, TESTED, NEEDS CONFIGURATION, NOT IMPLEMENTED or BLOCKED.
