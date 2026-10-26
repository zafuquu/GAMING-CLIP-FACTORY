ALTER TABLE rendered_outputs ADD COLUMN thumbnail_path TEXT;
ALTER TABLE rendered_outputs ADD COLUMN thumbnail_sha256 TEXT;
ALTER TABLE rendered_outputs ADD COLUMN thumbnail_width INTEGER;
ALTER TABLE rendered_outputs ADD COLUMN thumbnail_height INTEGER;
