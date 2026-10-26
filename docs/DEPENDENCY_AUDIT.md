# Dependency audit

- FastAPI, Pydantic v2, Uvicorn: API/request validation/server.
- Python sqlite3: persistent local database and migrations.
- React, React DOM, TypeScript, Vite, lucide-react: dashboard UI/build.
- FFmpeg/FFprobe binaries: optional local media analysis; install separately and configure.
- Remotion: not integrated; licensing and version compatibility must be reviewed before adoption.
- Official YouTube APIs/OAuth: not integrated; requires credentials, quota and policy review.

No FFmpeg binaries are committed. Gyan identifies its static builds as GPLv3; inspect actual package notices and obligations before redistributing.
