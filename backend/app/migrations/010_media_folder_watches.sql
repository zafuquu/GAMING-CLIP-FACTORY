CREATE TABLE IF NOT EXISTS media_folder_watches(
 id TEXT PRIMARY KEY,
 source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
 relative_path TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1,
 auto_analyze INTEGER NOT NULL DEFAULT 1,
 scan_interval_seconds INTEGER NOT NULL DEFAULT 30,
 next_scan_at TEXT,
 last_scan_at TEXT,
 last_error TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(source_id,relative_path)
);
CREATE INDEX IF NOT EXISTS idx_folder_watches_due ON media_folder_watches(enabled,next_scan_at);
CREATE TABLE IF NOT EXISTS watched_media_files(
 id TEXT PRIMARY KEY,
 watch_id TEXT NOT NULL REFERENCES media_folder_watches(id) ON DELETE CASCADE,
 relative_path TEXT NOT NULL,
 observed_size INTEGER NOT NULL,
 observed_mtime_ns INTEGER NOT NULL,
 stable_since TEXT NOT NULL,
 first_seen_at TEXT NOT NULL,
 last_seen_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING_STABILITY',
 media_id TEXT REFERENCES media_assets(id) ON DELETE SET NULL,
 error TEXT NOT NULL DEFAULT '',
 UNIQUE(watch_id,relative_path)
);
CREATE INDEX IF NOT EXISTS idx_watched_media_status ON watched_media_files(watch_id,status,last_seen_at);
