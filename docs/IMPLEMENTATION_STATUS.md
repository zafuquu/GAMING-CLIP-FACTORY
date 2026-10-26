# Implementation status

Release baseline: 0.1.x. This is a tested local production slice, not the completed autonomous content factory.

## Implemented

- FastAPI local API, SQLite persistence and numbered incremental schema migrations.
- Source registry and explicit rights gates. New sources start UNKNOWN; authorization requires license description, permission evidence, editing and commercial-use permission, and owner review.
- Rights expiration displayed as EXPIRED and checked at media import, analysis queueing, and worker execution.
- Local media registration inside configured media root, extension validation, SHA-256 identity and duplicate prevention.
- Persistent SQLite job queue, a background worker and finite subprocess timeouts.
- FFprobe metadata extraction and FFmpeg scene-change candidate generation. These are heuristics and require human review.
- Candidate approve/reject endpoint and dashboard page.
- Approved candidate render job: FFmpeg cuts the selected interval, preserves the complete frame in a 1080x1920 fit-and-pad layout, encodes H.264/AAC as available, records output hash and metadata, and runs deterministic QC for file size, video dimensions, duration and audio preservation.
- Render outputs/QC reports are persisted and displayed in the dashboard.
- Global pause, retry limits, event log and real database-backed dashboard.
- CI pipeline: backend API/rights tests on Windows/Linux with Python 3.11/3.12; frontend TypeScript/production build; Linux FFmpeg render integration test.

## Latest evidence observed

- GitHub Actions run on commit 85f66fb2 passed the backend matrix and frontend build. On Ubuntu/Python 3.12, the FFmpeg render integration test ran and all 11 tests passed.
- A subsequent CI run added explicit FFmpeg installation to the Linux test job; its Ubuntu/Python 3.12 job also passed with the media integration test. The run's remaining jobs were still completing when this file was last updated.
- The target Windows PC's full acceptance workflow is not yet verified. The user previously reported FFmpeg/FFprobe version checks passed but their separate manual validator stopped on PowerShell stderr handling. Do not claim their Windows installation is fully verified.

## Not implemented / not verified

- Automated discovery providers and authorized source ingestion.
- Semantic/game-specific highlight recognition, ranking model and candidate scoring explanations beyond scene-change heuristics.
- Content director, transcription, captions/narration, thumbnails and template variety.
- Remotion rendering.
- Advanced QC, repair, preview, quarantine and black/frozen-frame analysis.
- Official YouTube OAuth, upload/reconciliation, scheduled publication, quota handling and analytics.
- Autonomous public publishing.
- End-to-end automation across real licensed footage and a real YouTube channel.

Do not claim the business is fully automated. Public publishing remains disabled until provider permissions, credentials, safety gates and end-to-end acceptance tests are complete.
