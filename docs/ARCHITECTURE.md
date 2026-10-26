# Architecture

- React/TypeScript/Vite dashboard reads API data; it is not authoritative state.
- FastAPI listens on 127.0.0.1:8765 by default.
- SQLite persists source rights, media assets, candidates, jobs, settings and system events.
- A single worker consumes SQLite jobs and uses FFprobe/FFmpeg via argument arrays, shell=False, captured output and finite timeouts.
- Candidate windows from the FFmpeg scene-change filter are heuristics only and require human review.

Flow: manual source registration -> permission evidence and owner review -> local authorized media registration -> queued analysis -> candidate windows marked NEEDS_REVIEW. YouTube publishing is not integrated and has no endpoint in this release.
