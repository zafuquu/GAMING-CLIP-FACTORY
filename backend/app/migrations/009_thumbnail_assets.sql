ALTER TABLE rendered_outputs ADD COLUMN thumbnail_path TEXT;
ALTER TABLE rendered_outputs ADD COLUMN thumbnail_sha256 TEXT;
ALTER TABLE rendered_outputs ADD COLUMN thumbnail_status TEXT NOT NULL DEFAULT 'NOT_GENERATED';
ALTER TABLE rendered_outputs ADD COLUMN thumbnail_error TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_rendered_thumbnail_status ON rendered_outputs(thumbnail_status,created_at);
