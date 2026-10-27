ALTER TABLE content_plans ADD COLUMN narration_audio_path TEXT;
ALTER TABLE content_plans ADD COLUMN narration_audio_sha256 TEXT;
ALTER TABLE content_plans ADD COLUMN narration_audio_duration_seconds REAL;
ALTER TABLE content_plans ADD COLUMN narration_audio_status TEXT NOT NULL DEFAULT 'NOT_GENERATED';
ALTER TABLE content_plans ADD COLUMN narration_audio_error TEXT NOT NULL DEFAULT '';
ALTER TABLE content_plans ADD COLUMN narration_audio_generated_at TEXT;
CREATE INDEX IF NOT EXISTS idx_content_plans_narration_status ON content_plans(narration_audio_status,updated_at);
