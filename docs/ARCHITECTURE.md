# Architecture

- React/TypeScript/Vite dashboard reads API data; the database is authoritative.
- FastAPI listens on 127.0.0.1:8765 by default.
- SQLite stores source rights, media assets, candidates, jobs, rendered outputs, settings and events. Numbered SQL migrations are applied incrementally.
- A single worker consumes SQLite jobs. It uses FFprobe/FFmpeg argument arrays with shell=False, captured output and finite subprocess timeouts.
- Rights status and expiration are rechecked at decision, import/queue and worker execution boundaries.
- FFmpeg scene-change detection creates candidate windows. It is not semantic highlight recognition.
- An approved candidate queues a render job. The worker validates boundaries against source duration, renders a portrait fit-and-pad MP4 to data/outputs, probes it, applies deterministic QC, hashes the file and records a QC report.
- Output rendering does not imply permission to publish; all outputs remain local. No YouTube publisher is present.

Flow: manually register source → review permission → register local media → analyze → review candidates → approve/reject → render/QC → local output library. External discovery, AI content planning, Remotion, YouTube publishing and analytics remain future phases.
