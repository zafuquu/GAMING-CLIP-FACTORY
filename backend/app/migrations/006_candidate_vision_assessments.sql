CREATE TABLE IF NOT EXISTS candidate_vision_assessments(
 id TEXT PRIMARY KEY,
 candidate_id TEXT NOT NULL UNIQUE REFERENCES candidate_moments(id) ON DELETE RESTRICT,
 model_name TEXT NOT NULL,
 status TEXT NOT NULL,
 gameplay_context TEXT NOT NULL DEFAULT '',
 event_type TEXT NOT NULL DEFAULT 'unknown',
 visible_event TEXT NOT NULL DEFAULT '',
 is_actionable_highlight INTEGER NOT NULL DEFAULT 0,
 highlight_score REAL NOT NULL DEFAULT 0,
 confidence REAL NOT NULL DEFAULT 0,
 evidence_json TEXT NOT NULL DEFAULT '[]',
 quality_issues_json TEXT NOT NULL DEFAULT '[]',
 frame_times_json TEXT NOT NULL DEFAULT '[]',
 raw_response_json TEXT NOT NULL DEFAULT '{}',
 error TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidate_vision_status_score ON candidate_vision_assessments(status,highlight_score,confidence);
