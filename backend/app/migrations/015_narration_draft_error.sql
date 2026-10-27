ALTER TABLE content_plans ADD COLUMN narration_draft_error TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_content_plans_narration_draft_status ON content_plans(narration_draft_status,updated_at);
