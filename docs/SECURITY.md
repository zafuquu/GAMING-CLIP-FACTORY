# Security notes

- Bind API to loopback. Do not expose it to a LAN or internet without authentication, CSRF/origin controls and a security review.
- Do not commit secrets. Keep .env, OAuth Desktop client JSON, databases, media/output assets, model files and cache out of Git.
- Twitch credentials and YouTube API keys belong in local .env. YouTube OAuth tokens are stored in the OS keyring and are not logged or printed.
- Provider search returns metadata only. A public URL, API result or thumbnail is not reuse permission; discovered sources are UNKNOWN until verified rights evidence is reviewed.
- Only current, unexpired AUTHORIZED sources can enter the analysis/render/upload/public-release path. Only QC PASS outputs may be uploaded.
- Media paths must resolve inside the configured media root. Subprocesses use argument arrays, shell=False and finite timeouts.
- Vision inference is off by default. It samples limited frames and sends them to GCF_VISION_BASE_URL. Remote hosts are blocked unless GCF_VISION_ALLOW_REMOTE=true is explicitly set; only enable this after deciding that the selected endpoint may receive potentially private gameplay images. Model scores are uncalibrated and never auto-approve a candidate.
- Optional Faster-Whisper runs locally; model downloads are off by default.
- Uploads start private. Scheduled public release is a separate environment gate and repeats rights, hash, QC and remote processing checks immediately before publishing.
- Uncertain YouTube upload/privacy outcomes are marked RECONCILE_REQUIRED rather than blindly retried.
- Global pause prevents new work being claimed/queued but does not immediately kill an active subprocess.
- Back up SQLite, media and outputs independently. Public uploads/public scheduling are disabled by default.
