ALTER TABLE published_videos ADD COLUMN remote_processing_status TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE published_videos ADD COLUMN processing_checked_at TEXT;
ALTER TABLE published_videos ADD COLUMN processing_failure_reason TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_published_processing_status ON published_videos(remote_processing_status,publishing_status);
