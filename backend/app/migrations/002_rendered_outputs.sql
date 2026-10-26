CREATE TABLE IF NOT EXISTS rendered_outputs(
 id TEXT PRIMARY KEY,
 candidate_id TEXT NOT NULL REFERENCES candidate_moments(id) ON DELETE RESTRICT,
 media_id TEXT NOT NULL REFERENCES media_assets(id) ON DELETE RESTRICT,
 output_path TEXT NOT NULL UNIQUE,
 file_size INTEGER NOT NULL,
 sha256 TEXT NOT NULL,
 duration_seconds REAL NOT NULL,
 width INTEGER NOT NULL,
 height INTEGER NOT NULL,
 video_codec TEXT NOT NULL,
 audio_codec TEXT,
 qc_status TEXT NOT NULL,
 qc_report_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rendered_outputs_candidate ON rendered_outputs(candidate_id,created_at);
CREATE INDEX IF NOT EXISTS idx_candidates_state_created ON candidate_moments(state,created_at);
