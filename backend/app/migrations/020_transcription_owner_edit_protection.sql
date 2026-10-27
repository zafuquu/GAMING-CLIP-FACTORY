ALTER TABLE content_plans ADD COLUMN owner_edited INTEGER NOT NULL DEFAULT 0;
UPDATE content_plans SET owner_edited=1;

ALTER TABLE caption_segments ADD COLUMN manually_edited INTEGER NOT NULL DEFAULT 0;
UPDATE caption_segments SET manually_edited=1;
