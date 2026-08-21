ALTER TABLE academy_groups
    DROP CONSTRAINT IF EXISTS academy_groups_level_check;

ALTER TABLE academy_groups
    ADD COLUMN IF NOT EXISTS levels TEXT[];

UPDATE academy_groups
SET levels = ARRAY[level]
WHERE levels IS NULL
  AND level IS NOT NULL;

ALTER TABLE academy_groups
    ADD CONSTRAINT academy_groups_levels_check
    CHECK (
        levels IS NULL
        OR levels <@ ARRAY['Beginner', 'Intermediate', 'Advanced']::TEXT[]
    );
