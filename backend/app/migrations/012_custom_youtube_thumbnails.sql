ALTER TABLE published_videos ADD COLUMN custom_thumbnail_path TEXT;
ALTER TABLE published_videos ADD COLUMN custom_thumbnail_sha256 TEXT;
ALTER TABLE published_videos ADD COLUMN custom_thumbnail_width INTEGER;
ALTER TABLE published_videos ADD COLUMN custom_thumbnail_height INTEGER;
ALTER TABLE published_videos ADD COLUMN custom_thumbnail_status TEXT NOT NULL DEFAULT 'NOT_GENERATED';
ALTER TABLE published_videos ADD COLUMN custom_thumbnail_error TEXT NOT NULL DEFAULT '';
ALTER TABLE published_videos ADD COLUMN custom_thumbnail_uploaded_at TEXT;
CREATE INDEX IF NOT EXISTS idx_published_thumbnail_status ON published_videos(custom_thumbnail_status,updated_at);
