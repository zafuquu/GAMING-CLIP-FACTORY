# Implementation status

Release baseline: 0.3 development alpha. The repository now contains a substantial local pipeline and optional provider integrations, but it is **not fully hands-off end-to-end on a new machine**.

## Implemented in code

- FastAPI local API, SQLite persistence, numbered migrations 001–006, persistent jobs and worker startup recovery.
- Source registry with license/permission evidence, editing/commercial-use flags, owner review and expiration checks.
- Local media registration restricted to the configured media root, with supported-extension checks, SHA-256 and duplicate prevention.
- Twitch Helix metadata discovery and YouTube Data API public metadata search, including persisted schedules and discovery history. Results are metadata only and always start with rights UNKNOWN.
- FFprobe metadata and FFmpeg scene-change candidate generation. This is heuristic candidate discovery, not semantic gameplay recognition.
- Optional local Faster-Whisper transcript and timed phrase segments, with deterministic transcript-grounded content plan drafts and caption timing.
- Optional local Ollama-compatible vision adapter: samples three frames per candidate, asks for structured event/evidence estimates, records score/model/confidence and adjusts explainable candidate ranking. Default off; local loopback preferred; remote endpoint blocked unless explicitly enabled. Scores are explicitly uncalibrated, and candidates remain human-review-only.
- Candidate approval/rejection, H.264/AAC portrait fit-and-pad rendering, optional SRT sidecars/subtitle burn-in, output registry and SHA-256.
- Deterministic QC covers probe/decode, dimensions, duration, codecs/audio, black-frame and frozen-frame heuristics. Warning cases become REVIEW and are not uploadable; only PASS output is eligible for publishing.
- Preview support for PASS and REVIEW outputs, stored transcript/content plan/vision evidence in API and dashboard.
- YouTube Desktop OAuth stored in OS keyring; private resumable uploads, upload history, uncertain outcome reconciliation, processing-status polling, Analytics API snapshots, and separately gated scheduled-public release worker. Before public release, the worker rechecks current rights, output hash/size, QC and live remote processing/privacy status.
- Global pause, bounded subprocess timeouts, retry/backoff, startup recovery and non-blind-retry handling for uncertain external operations.
- CI workflow: backend tests on Windows/Linux and Python 3.11/3.12, synthetic FFmpeg render/QC on Linux, and frontend typecheck/build.

## Configuration and acceptance still required

- Twitch developer app credentials; YouTube Data API key or OAuth connection.
- Google Cloud OAuth Desktop client, channel-owner authentication and the required scopes for uploads, video privacy updates and Analytics.
- Any source: authentic permission evidence granting contemplated editing/commercial reuse. Discovery results do not grant reuse permission.
- Optional speech model package and model files; model downloads are off unless explicitly enabled.
- Optional local Ollama server and compatible vision model; vision is disabled by default and does not equal a validated game-event detector.
- Real private YouTube upload, processing-status polling, OAuth scope verification, Analytics sync and uncertain-outcome reconciliation on the user's actual channel.
- Windows end-to-end acceptance against the user's exact FFmpeg/FFprobe installation. Hosted CI success is not a substitute.

## What remains incomplete

- Broadly reliable game-aware visual event recognition across multiple titles. The optional vision model sees three sparse frames and may miss motion, context, HUD meaning or events; its scores are subjective and uncalibrated. It needs labeled, licensed footage and game-specific evaluation before use as a production ranker.
- Caption correction/review UI, high-quality narration, thumbnail generation, richer template/composition system, safe-area checking and sophisticated automatic repair/quarantine.
- Automatic acquisition of source media. Current providers search metadata only; legally authorized local media or documented grants are still required.
- Production deployment hardening (authentication beyond loopback, high concurrency, robust provider quota budgeting, notifications, disk-space guard, cancellation and extensive Windows stress tests).
- Live third-party credentials and the complete real-channel/rights-cleared-footage end-to-end acceptance workflow.
- Unattended autonomous publishing is not enabled by default and must not be described as operational before supervised acceptance passes.

## Current verification standard

The last previously observed all-green Actions run was on commit 92c03c19, before the newest vision adapter and its integration tests. Later changes have their own Actions runs; check the latest main-head run before claiming all current tests are green. Tests use local fakes for provider calls and synthetic media; they do not prove real API credentials or legal permissions.

Status vocabulary: IMPLEMENTED (code exists), TESTED (reproducible test passed), NEEDS CONFIGURATION (owner credentials/config required), NOT IMPLEMENTED or BLOCKED. Code presence alone does not prove the integration works end-to-end.
