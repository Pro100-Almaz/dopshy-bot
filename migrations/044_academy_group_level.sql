ALTER TABLE academy_groups
    ADD COLUMN IF NOT EXISTS level TEXT;

ALTER TABLE academy_groups
    ADD CONSTRAINT academy_groups_level_check
    CHECK (level IS NULL OR level IN ('Beginner', 'Intermediate', 'Advanced'));
