ALTER TABLE rendered_outputs ADD COLUMN render_profile TEXT NOT NULL DEFAULT 'fit_pad';
CREATE INDEX IF NOT EXISTS idx_rendered_profile_created ON rendered_outputs(render_profile,created_at);
