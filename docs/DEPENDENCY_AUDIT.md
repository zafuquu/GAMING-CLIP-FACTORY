# Dependency audit

## Dependency reproducibility

The lock matrix has one hash-pinned backend file for each supported runtime: `backend/requirements-lock-{linux,win}-{py311,py312}.txt`. Optional speech-recognition locks follow the same naming pattern as `requirements-ml-lock-{linux,win}-{py311,py312}.txt`, constrained to the matching standard lock because Faster-Whisper/CTranslate2 adds platform-specific wheels and dependencies. The Windows installer selects the matching lock based on the Python in `.venv`; CI selects by matrix cell; the frontend is installed from `frontend/package-lock.json` via `npm ci`. `scripts/validate_dependency_locks.py` checks direct requirement ranges, SHA-256 presence, the standard/ML pin relationship, and npm root metadata. Startup fingerprints include the input manifests as well as the selected lock files; a stale lock matrix is rejected instead of being marked installed. Use GitHub Actions → **Refresh dependency locks** to generate new OS/Python-specific lock artifacts with pip-tools 7.6.2, then follow `docs/DEPENDENCY_LOCK_MAINTENANCE.md` to review and commit all affected locks with manifest changes. Never use a generic platform-agnostic hash lock for these dependencies. Locking does not replace license, security-advisory or runtime-compatibility review.

## Required Python runtime

- FastAPI, Pydantic v2, Uvicorn: API and request validation.
- python-dotenv: local .env configuration.
- requests: Twitch/vision HTTP clients.
- google-api-python-client, google-auth-oauthlib, google-auth-httplib2: YouTube Data/Analytics, OAuth and bounded authenticated HTTP requests.
- keyring: stores YouTube OAuth credentials in the operating-system credential store.
- pytest and httpx: tests.
- Pillow: deterministic cover-art composition, JPEG integrity checks and dimension validation.
- Python sqlite3, subprocess, hashlib, tempfile and other standard-library modules: persistence, media commands, hashes and temporary frame extraction.

## External/local processes

- FFmpeg/FFprobe: media analysis, frame sampling, rendering profiles, subtitle handling, poster extraction and QC. Install separately. Never commit binaries without checking distribution/license obligations. Gyan identifies its static builds as GPLv3.
- Ollama-compatible local vision server: optional, disabled by default, no Python Ollama SDK required. Default API is loopback at http://127.0.0.1:11434/api/chat. A vision model and its weights must be installed separately; review license, hardware needs and accuracy. Remote endpoints are blocked unless explicitly allowed.
- Twitch Helix: requires a Twitch developer app and client credentials for official metadata discovery.
- YouTube Data API v3: public search can use GCF_YOUTUBE_API_KEY; this does not authenticate channel ownership or grant reuse rights.
- YouTube upload/privacy changes/Analytics/custom thumbnail upload: require Google Cloud project, OAuth Desktop client, owner authentication, scopes and current quota/account-policy review. Custom thumbnail upload is implemented via the official YouTube Data API; account eligibility for custom thumbnails is an external prerequisite.
- Faster-Whisper: optional local speech recognition from backend/requirements-ml.txt; weights are not bundled and model downloads are disabled unless explicitly opted in.
- Piper CLI: optional separately installed local text-to-speech executable plus an ONNX voice model and matching JSON config. The app does not download or bundle either. Review the current engine license and the specific voice model/dataset card independently; voice licenses differ.
- Remotion: not integrated; compatibility and licensing review required before adoption.

An integration is not operational until real credentials/setup and end-to-end acceptance checks are completed.
