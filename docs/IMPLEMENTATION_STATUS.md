# Implementation status

Release baseline: 0.1.0. This is a foundation, not the completed autonomous content factory.

## Implemented
- FastAPI local API, SQLite schema and migration.
- Source registry and explicit rights gate. New sources start UNKNOWN; AUTHORIZED requires evidence, license description, editing and commercial-use permission, and owner review.
- Media registration inside configured media root, supported extension checks, SHA-256 identity and duplicate prevention.
- Persistent SQLite job queue, one worker, finite subprocess timeouts, FFprobe metadata, FFmpeg scene-change review candidates.
- Dashboard endpoints, global pause, bounded retry policy and system events.
- React/TypeScript dashboard source.

## Not implemented / not verified
- Automated discovery providers, semantic/game-specific highlight recognition, learned ranking, AI content director.
- Remotion rendering, captions/narration, thumbnails, automated QC repair.
- YouTube OAuth/upload/scheduling/reconciliation/analytics.
- Windows end-to-end tests with the user's FFmpeg installation.
- Frontend npm build in hosted environment; npm registry was unreachable.

Do not claim end-to-end automation or enable autonomous public publishing until later phases and acceptance tests are complete.
