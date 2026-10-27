-- Weekdays (0=Mon .. 6=Sun, same as academy_group_schedules.training_day) on
-- which a group takes no trial lessons. NULL / empty = no restriction.
ALTER TABLE academy_groups
    ADD COLUMN IF NOT EXISTS ignore_days SMALLINT[];

ALTER TABLE academy_groups
    DROP CONSTRAINT IF EXISTS academy_groups_ignore_days_check;
ALTER TABLE academy_groups
    ADD CONSTRAINT academy_groups_ignore_days_check
    CHECK (ignore_days IS NULL OR ignore_days <@ ARRAY[0, 1, 2, 3, 4, 5, 6]::SMALLINT[]);

-- Football groups: no trials on Friday (4) and Saturday (5).
UPDATE academy_groups
SET ignore_days = ARRAY[4, 5]::SMALLINT[]
WHERE group_type = 'football';
