# Production Readiness Audit

**Assessment date:** 27 October 2026  
**Repository:** `zafuquu/GAMING-CLIP-FACTORY`  
**Current conclusion:** **Not yet certified for unattended production.** The project is a substantial supervised local-first alpha. A green CI run validates tests under its runner conditions; it does not prove that the target Windows machine, configured providers, rights evidence or real YouTube channel are ready.

## Scope reviewed

Reviewed the repository tree and current product documentation, architecture/implementation status, environment and security guidance, Windows setup/run scripts, GitHub Actions workflow, FastAPI application and configuration, the persistent worker and scheduler, source/rights and media intake paths, rendering/vision/narration/YouTube integration modules, and tests for API behavior, rights gating, rendering, storage safety, processing, publication, reconciliation and analytics. The audit focused on operational paths and safety-critical state transitions rather than assuming that code presence proves the feature works.

## Verified repository hardening (as of 27 October 2026)

The following changes are merged into main and covered by GitHub Actions run #364 (all six jobs green): [run details](https://github.com/zafuquu/GAMING-CLIP-FACTORY/actions/runs/40478057648).

- `scripts/start.bat` is the one-click local entry point. It checks supported Python/Node versions and dependency health, creates `.env` only if absent, installs or repairs missing dependencies, builds the optimized dashboard, starts/reuses the loopback API and waits for both API and UI to respond.
- The API serves the built React bundle from the same local process. The Vite development server remains available through the separate developer script.
- `/api/readiness` checks SQLite, executable FFmpeg/FFprobe version responses, writable database/media/output locations, and worker liveness. Missing media tools produce an explicit degraded result rather than a false ready result.
- The worker continues retrying after an operational failure even if writing the error event also fails; the secondary failure is written to stderr.
- Scheduled public release revalidates rights, schedule, publishing policy, output/QC linkage, file size/hash and global pause after the live YouTube status lookup and before making a video public.
- Windows CI installs FFmpeg so that media-integration tests can exercise Windows rather than silently skip because the binary is absent.
- Windows startup CI runs the production launcher, verifies API/dashboard/readiness, starts the app a second time to assert one backend process remains, invokes the stop command, and confirms the API is no longer responding.
- A synthetic-media mock-production acceptance test exercises watched-folder import, scene analysis, capped supervised auto-render, FFmpeg render/QC, private YouTube-upload state using a mocked provider, and mocked processing success. It verifies that a private upload does not queue public release.
- The Windows stop command terminates the owned backend process tree so an active FFmpeg child is not left running after a normal stop.
- Regression tests cover healthy/degraded readiness, serving the built dashboard, worker-error logging failure, a live worker, and rights/artifact/schedule changes during the remote status lookup.

**Verification rule:** a result applies only to its exact commit. The cited main-branch run verifies the merged start/stop hardening and mock-production tests; changes made afterward require another successful run. Hosted CI still does not certify the actual target PC or real external credentials.

## What the application can and cannot automate safely

After one-time setup, folder watches can import stable local files associated with an explicitly authorized source and queue analysis. Metadata searches through official Twitch/YouTube APIs discover metadata, not source media files. Public availability is not reuse permission.

Candidate generation is currently based on FFmpeg scene changes and optional transcription/sparse-frame vision. The vision score is uncalibrated; scene boundaries do not reliably identify a clutch, kill, win, reaction or other meaningful game moment. Candidate review and approval must remain a human gate until a game-specific classifier is evaluated on representative, rights-cleared and human-labeled footage.

The current UI supports candidate approval, rendering/QC, YouTube publication workflows, private upload/processing, reconciliation, analytics, and separately gated public release. **A single start action cannot safely eliminate first-time permission/credential setup, select rights by inference, or guarantee that generated clips are good.** The one-click launcher starts the local services; it is not a legal source-acquisition mechanism and does not automatically grant approval or enable public publishing.

## Release blockers / external acceptance

| Gate | Status | Evidence required before production |
|---|---|---|
| Windows launch and ports | NEEDS TARGET-MACHINE TEST | Fresh checkout on the intended Windows PC; run `scripts/start.bat`; verify API, bundled UI, logs, clean stop/restart, and no port conflict. |
| Media toolchain | NEEDS CONFIGURATION | Install a supported FFmpeg/FFprobe build; `/api/readiness` must report both executables working. Validate on the same machine/build used for production. |
| Rights and input footage | NEEDS OWNER ACTION | Record explicit evidence allowing intended editing and commercial reuse for each source. Test with synthetic or properly licensed footage. Never infer authorization from a public URL. |
| Twitch discovery | NEEDS CONFIGURATION | Valid developer credentials, actual API request, quota/error behavior and saved schedule tested. Discovery is metadata-only. |
| YouTube publishing and Analytics | NEEDS CONFIGURATION + LIVE TEST | Configure Google OAuth Desktop credentials, authenticate the intended channel, confirm required scopes, upload a private test video, poll processing, retry/reconcile a forced interruption without duplicates, verify thumbnail behavior and Analytics ingestion. |
| Highlight quality | NOT YET CALIBRATED | Label representative clips across target games; measure precision/recall and false positives; require user review for low-confidence results. Do not switch to automatic approvals based only on the current score. |
| Public release | DISABLED BY DEFAULT / NEEDS ACCEPTANCE | First validate private upload and processing; then test pause, expiry/revocation, output-hash/QC mismatch, schedule changes, cancellation and reconciliation. Only the owner should enable a separate public-release policy. |
| Dependency reproducibility | IMPLEMENTED ON PR BRANCH / NEEDS CI AND UPDATE POLICY | A separate unmerged branch has generated four hash-pinned backend locks and four optional ML locks (Windows/Linux × Python 3.11/3.12), an npm lockfile, Windows lock fingerprints, and lock-consuming install paths. Do not rely on them until the exact PR head passes the full matrix and is merged. A maintained lock-generation path and scheduled advisory/dependency-update process remain outstanding. |
| Long-running operations | NEEDS STRESS / CHAOS TESTS | Test database interruption/recovery, disk-full recovery, process restart while FFmpeg is active, worker queue fairness, API quota exhaustion, timeouts and uncertain remote outcomes. Establish backup/restore and alerting procedures. |
| Security boundary | LOCAL-ONLY | API is intended to bind to loopback. Do not expose it to a LAN, reverse proxy or internet without a separate authentication/CSRF/security design review and tests. Keep OAuth files, tokens, environment secrets, source files and rendered outputs out of Git. |
| Video quality | PARTIAL | Existing deterministic QC does not replace listening/watching rendered clips; add/validate subtitle safe areas, audio loudness/clipping, readability, codec/player compatibility and game-specific composition before unattended output. |

## Acceptance runbook

1. On the target Windows machine, preserve existing `data/`, `media/`, `.env`, OAuth keyring entries and outputs. Install Python 3.11 or 3.12 and Node.js 20.19+ or 22.12+, then run `scripts/start.bat`.
2. Verify `http://127.0.0.1:8765/api/health`, `/api/readiness`, dashboard rendering, worker status and app restart. Readiness must be fully ready for the local media pipeline before real jobs are queued.
3. Import synthetic/authorized footage through a source whose rights evidence has been explicitly recorded. Run discovery/analysis, inspect the evidence and preview the candidate, approve it, render it and require QC PASS. Watch for missing audio, malformed captions, wrong aspect ratio, visual crop mistakes and encoding artifacts.
4. With a test/owner-controlled YouTube channel, exercise private upload, processing, thumbnail, reconciliation, analytics and controlled retry after a deliberately interrupted operation. Confirm the same render never produces duplicate uploads.
5. Keep public release disabled while testing. Use mocked tests to verify expired/revoked rights, a changed render, a rescheduled publication and global pause all prevent an unsafe public-state mutation. Only after those tests and the live private-channel acceptance pass should an owner consider enabling public release, with conservative limits and ongoing manual review.
6. Test a backup and restore of the SQLite database and required media/output files. Confirm there is a documented recovery path for interrupted jobs and uncertain YouTube responses.

## Definition of done

Production acceptance requires the latest full Python 3.11/3.12 Windows/Linux matrix and frontend build to pass, no unexplained failures/skips in media integration tests, the target-PC smoke test to pass, the actual authorized-media render test to pass, and a real private YouTube lifecycle/reconciliation/Analytics test to pass. All credentials, rights, quotas, backup/restore and operational controls must be configured and recorded. Public release remains a separate opt-in gate.

There is no honest way to guarantee “100000% no issues.” This checklist makes remaining uncertainty explicit and prevents a successful unit-test run from being mistaken for proof of the complete live production workflow.
