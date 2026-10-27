# Dependency audit

## Dependency reproducibility

The lock matrix has one hash-pinned backend file for each supported runtime: `backend/requirements-lock-{linux,win}-{py311,py312}.txt`. Optional speech-recognition locks follow the same naming pattern as `requirements-ml-lock-{linux,win}-{py311,py312}.txt` and are compiled with the matching standard lock as constraints, so enabling speech recognition does not silently change the base runtime stack. The Windows installer selects the lock matching the Python in `.venv`; CI selects by matrix cell; the frontend is installed from `frontend/package-lock.json` via `npm ci`. `scripts/validate_dependency_locks.py` verifies the direct manifest ranges, per-package SHA-256 hashes, standard/ML pin parity, and npm lock root metadata in each matrix cell and before either Windows installer proceeds. Startup fingerprints include both manifests and selected lockfiles, so stale manifests cause a repair instead of being silently treated as installed. The scheduled/manual refresh workflow uses pinned `pip-tools==7.6.2`, generates artifacts on the relevant OS/Python combinations, and validates them before opening a dependency-update PR. Never use a generic platform-agnostic hash lock for these dependencies. Locking does not replace license, security-advisory or runtime-compatibility review.

## Required Python runtime

- FastAPI, Pydantic v2, Uvicorn: API and request validation.
- python-dotenv: local .env configuration.
- requests: Twitch/vision HTTP clients.
- google-api-python-client, google-auth-oauthlib, google-auth-httplib2: YouTube Data/Analytics, OAuth and bounded authenticated HTTP requests.
- keyring: stores YouTube OAuth credentials in the operating-system credential store.
- pytest and httpx: tests.
- Pillow: deterministic cover-art composition, JPEG integrity checks and dimension validation.
- Python sqlite3, subprocess, hashlib, tempfile and other standard-library modules: persistence, media commands, hashes and temporary frame extraction.

## Security lock audit result

- The initial hashed lock set pinned Pillow 12.3.0. The scheduled audit correctly failed and reported 37 known advisory records for that version across the base and optional-ML lock variants; the reported fix was Pillow 13.0.0.
- The source requirement now requires `Pillow>=13.0.0,<14.0.0`. The refresh workflow regenerated all eight Windows/Linux × Python 3.11/3.12 base and optional-ML lock files, each pinning Pillow 13.0.0.
- The new lock set passed all eight `pip-audit` checks plus `npm audit` in [dependency-security run #7](https://github.com/zafuquu/GAMING-CLIP-FACTORY/actions/runs/40492166354), on commit `08c430b79eeea85567ad9384f4bd2473382d98c5`.
- Pillow 13.0.0 security fixes and supported Python versions are documented in the [official release notes](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html). The repo’s app/test matrix is still required on the exact final PR head; an audit pass is not a substitute for runtime tests.

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


## Update and vulnerability review

`.github/dependabot.yml` monitors GitHub Actions and frontend npm dependencies. `.github/workflows/refresh-dependency-locks.yml` (scheduled and manually triggerable) regenerates the four OS/Python backend locks and four optional-ML locks on matching runners, refreshes `frontend/package-lock.json`, and opens a reviewable PR instead of pushing changes directly to `main`. The separate scheduled security workflow audits locked Python base/optional-ML dependency sets and the npm lock. Every generated update still requires the normal six-job acceptance workflow before merge; automatic merging is deliberately disabled.
