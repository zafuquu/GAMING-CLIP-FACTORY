# Security notes

- Bind API to loopback. Do not expose it to a LAN or internet without authentication, CSRF/origin controls and a security review.
- Do not commit secrets. .env.example contains blank settings; keep .env, OAuth Desktop client JSON, SQLite/media/output files and model cache out of Git.
- Twitch credentials and YouTube API keys belong in local .env. YouTube OAuth tokens are stored through the OS keyring and are not logged or printed.
- A public URL, API search result or thumbnail is not reuse permission. Provider discovery returns metadata only and leaves rights UNKNOWN.
- Only currently authorized and unexpired sources can enter import/analysis/render/upload. QC must be PASS before an output can be queued for upload.
- Media paths must resolve inside the configured media root. Worker subprocesses use argument arrays, shell=False and finite timeouts.
- A global pause prevents new work from being queued/claimed. It does not abruptly terminate an active process; media commands have bounded timeouts.
- Upload starts private. Scheduled public release has a separate explicit environment gate, is queued as a local job, and rechecks permission, output hash/size and QC immediately before public state change.
- A timeout or restart during an uncertain remote upload/public action results in RECONCILE_REQUIRED; never automatically blindly retry a non-idempotent upload.
- Optional Faster-Whisper runs locally. Model download is off by default. Model files/license and resource use must be reviewed.
- Back up SQLite, media and outputs separately. No public publishing is enabled by default.
