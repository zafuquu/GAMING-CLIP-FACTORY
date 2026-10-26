# Dependency audit

## Required runtime

- Python sqlite3: persistent local database and migrations; standard library.
- FastAPI, Pydantic v2, Uvicorn: API and request validation.
- python-dotenv: load local .env configuration.
- React, React DOM, TypeScript, Vite, lucide-react: dashboard UI and frontend build.
- FFmpeg/FFprobe: media analysis, encoding, subtitle/defect filters and QC. Binaries are installed separately; don't commit them. Review the actual build's license and notices. Gyan identifies its static builds as GPLv3.
- requests: Twitch Helix HTTP client.
- google-api-python-client and google-auth-oauthlib: YouTube Data/Analytics APIs and local OAuth flow.
- keyring: stores OAuth credential JSON in the operating system's credential store, not a repository file.

## Optional local AI

- faster-whisper (from backend/requirements-ml.txt): local speech recognition and timestamped segments. Model weights are not bundled. A model download requires explicit GCF_WHISPER_ALLOW_MODEL_DOWNLOAD=true, otherwise configure an existing GCF_WHISPER_MODEL_PATH. Review the model/weight terms and resource use.

## External services / setup requirements

- Twitch Helix requires a registered Twitch developer app and app credentials for client-credentials requests.
- YouTube public metadata search can use a Google Cloud YouTube Data API v3 key; search does not authenticate channel ownership or give reuse permission.
- Upload and analytics require a Google OAuth Desktop client, accepted consent configuration, owner authentication and required scopes/quotas. OAuth tokens are held in OS keyring.
- Remotion is not installed or integrated; licensing and current package compatibility must be reviewed before adoption.

No external provider is assumed operational until configured and tested against the actual credentials.
