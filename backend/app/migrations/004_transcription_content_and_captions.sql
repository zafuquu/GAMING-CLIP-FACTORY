ALTER TABLE candidate_moments ADD COLUMN ranking_score REAL;
ALTER TABLE candidate_moments ADD COLUMN ranking_components_json TEXT NOT NULL DEFAULT '{}';
CREATE TABLE IF NOT EXISTS transcripts(
 id TEXT PRIMARY KEY,
 media_id TEXT NOT NULL UNIQUE REFERENCES media_assets(id) ON DELETE RESTRICT,
 language TEXT NOT NULL DEFAULT 'unknown',
 language_probability REAL,
 full_text TEXT NOT NULL DEFAULT '',
 model_name TEXT NOT NULL,
 status TEXT NOT NULL,
 error TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transcript_segments(
 id TEXT PRIMARY KEY,
 transcript_id TEXT NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
 segment_index INTEGER NOT NULL,
 start_seconds REAL NOT NULL,
 end_seconds REAL NOT NULL,
 text TEXT NOT NULL,
 confidence REAL,
 UNIQUE(transcript_id,segment_index)
);
CREATE INDEX IF NOT EXISTS idx_transcript_segments_time ON transcript_segments(transcript_id,start_seconds,end_seconds);
CREATE TABLE IF NOT EXISTS content_plans(
 id TEXT PRIMARY KEY,
 candidate_id TEXT NOT NULL UNIQUE REFERENCES candidate_moments(id) ON DELETE RESTRICT,
 chosen_format TEXT NOT NULL,
 hook_text TEXT NOT NULL DEFAULT '',
 title TEXT NOT NULL,
 description TEXT NOT NULL DEFAULT '',
 tags_json TEXT NOT NULL DEFAULT '[]',
 narration_text TEXT NOT NULL DEFAULT '',
 evidence_json TEXT NOT NULL DEFAULT '{}',
 confidence REAL NOT NULL DEFAULT 0,
 strategy_version TEXT NOT NULL DEFAULT 'rules-v1',
 status TEXT NOT NULL DEFAULT 'DRAFT',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS caption_segments(
 id TEXT PRIMARY KEY,
 candidate_id TEXT NOT NULL REFERENCES candidate_moments(id) ON DELETE CASCADE,
 segment_index INTEGER NOT NULL,
 start_seconds REAL NOT NULL,
 end_seconds REAL NOT NULL,
 text TEXT NOT NULL,
 confidence REAL,
 created_at TEXT NOT NULL,
 UNIQUE(candidate_id,segment_index)
);
CREATE INDEX IF NOT EXISTS idx_caption_segments_candidate_time ON caption_segments(candidate_id,start_seconds,end_seconds);
