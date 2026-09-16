ALTER TABLE academy_group_schedules
    ADD COLUMN IF NOT EXISTS field INTEGER;

ALTER TABLE academy_group_schedules
    DROP CONSTRAINT IF EXISTS academy_group_schedules_field_check;

ALTER TABLE academy_group_schedules
    ADD CONSTRAINT academy_group_schedules_field_check
    CHECK (field IS NULL OR field BETWEEN 1 AND 3);

ALTER TABLE academy_groups
    DROP CONSTRAINT IF EXISTS academy_groups_level_check;

ALTER TABLE academy_groups
    DROP CONSTRAINT IF EXISTS academy_groups_levels_check;

ALTER TABLE academy_groups
    DROP COLUMN IF EXISTS level;

ALTER TABLE academy_groups
    RENAME COLUMN levels TO level;

ALTER TABLE academy_groups
    ADD CONSTRAINT academy_groups_level_check
    CHECK (
        level IS NULL
        OR level <@ ARRAY['Beginner', 'Intermediate', 'Advanced']::TEXT[]
    );
