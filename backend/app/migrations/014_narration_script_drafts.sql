ALTER TABLE content_plans ADD COLUMN narration_script_reviewed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE content_plans ADD COLUMN narration_draft_status TEXT NOT NULL DEFAULT 'NOT_GENERATED';
ALTER TABLE content_plans ADD COLUMN narration_draft_model TEXT;
ALTER TABLE content_plans ADD COLUMN narration_draft_evidence_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE content_plans ADD COLUMN narration_draft_generated_at TEXT;
CREATE INDEX IF NOT EXISTS idx_content_plans_script_review ON content_plans(narration_script_reviewed,narration_draft_status);
