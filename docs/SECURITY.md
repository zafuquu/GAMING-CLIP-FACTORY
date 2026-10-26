# Security notes

- Bind API to loopback. Do not expose it to a LAN/internet without authentication, CSRF/origin controls and security review.
- Never commit secrets. Keep .env, OAuth client JSON, databases, media/output files, model weights and caches outside Git.
- Twitch credentials and YouTube API keys stay in local .env. YouTube OAuth tokens are stored in OS keyring; a scope-version marker rejects stale authorizations after scope changes and requires reconnection.
- Provider search returns metadata only. A public URL, API result or thumbnail is not reuse permission; discovered source rights are UNKNOWN until verified.
- Only current, unexpired AUTHORIZED source permissions can enter analysis/render/upload/public release. Only QC PASS outputs may be uploaded.
- Media paths must resolve inside the configured media root. Subprocesses use argument arrays, shell=False and finite timeouts.
- Generated poster paths must resolve under the managed outputs directory; API validates registered dimensions and SHA-256 before serving the image.
- Vision is off by default. It may send sampled frames to GCF_VISION_BASE_URL; remote endpoints are blocked unless GCF_VISION_ALLOW_REMOTE=true. Consider whether the endpoint may receive private gameplay images. Vision scores are uncalibrated and never auto-approve.
- Optional Faster-Whisper runs locally; model download is disabled unless explicitly enabled.
- YouTube uploads start private. Public release has a separate environment gate and immediately rechecks rights/expiry, scheduled time, output hash/size, QC and live processing state.
- If reconciliation discovers public visibility that current permissions/policy do not allow, it tries to set the video private. Uncertain upload/privacy outcomes are RECONCILE_REQUIRED and never blindly retried.
- Global pause defers queue claims and checks immediately before outbound YouTube operations; it does not instantly kill an already-running FFmpeg process.
- Key SQLite transitions use immediate write locks. Migrations use unique version numbers and atomic schema/version writes; database uniqueness allows one publication record per rendered output.
- Back up SQLite, media and outputs independently. Public upload/release are disabled by default.
