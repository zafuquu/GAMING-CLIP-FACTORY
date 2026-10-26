ALTER TABLE jobs ADD COLUMN next_attempt_at TEXT;
ALTER TABLE sources ADD COLUMN external_source_id TEXT;
ALTER TABLE sources ADD COLUMN provider_metadata_json TEXT NOT NULL DEFAULT '{}';
CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_platform_external_id ON sources(platform, external_source_id) WHERE external_source_id IS NOT NULL;
ALTER TABLE published_videos ADD COLUMN rendered_output_id TEXT REFERENCES rendered_outputs(id);
ALTER TABLE published_videos ADD COLUMN scheduled_publish_at TEXT;
ALTER TABLE published_videos ADD COLUMN upload_request_started_at TEXT;
CREATE INDEX IF NOT EXISTS idx_published_status_created ON published_videos(publishing_status,created_at);
CREATE TABLE IF NOT EXISTS discovery_runs(
 id TEXT PRIMARY KEY,
 provider TEXT NOT NULL,
 query TEXT NOT NULL,
 status TEXT NOT NULL,
 found_count INTEGER NOT NULL DEFAULT 0,
 inserted_count INTEGER NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '',
 started_at TEXT NOT NULL,
 finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_discovery_runs_started ON discovery_runs(started_at);
