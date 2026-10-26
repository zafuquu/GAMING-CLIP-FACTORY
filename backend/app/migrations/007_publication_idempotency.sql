CREATE UNIQUE INDEX IF NOT EXISTS idx_published_rendered_output_unique ON published_videos(rendered_output_id) WHERE rendered_output_id IS NOT NULL;
