-- Trial intake fields. Reference/sample data belongs in seeds/, not migrations.
ALTER TABLE academy_groups
    ADD COLUMN IF NOT EXISTS birth_years INTEGER[];
ALTER TABLE academy_groups
    ADD COLUMN IF NOT EXISTS location TEXT;

ALTER TABLE academy_users
    ADD COLUMN IF NOT EXISTS child_birth_year INTEGER;
ALTER TABLE academy_users
    ADD COLUMN IF NOT EXISTS experience TEXT;
ALTER TABLE academy_users
    ADD COLUMN IF NOT EXISTS school_hours TEXT;
ALTER TABLE academy_users
    ADD COLUMN IF NOT EXISTS subscribed BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE academy_trials
    ADD COLUMN IF NOT EXISTS child_birth_year INTEGER;
ALTER TABLE academy_trials
    ADD COLUMN IF NOT EXISTS experience TEXT;
ALTER TABLE academy_trials
    ADD COLUMN IF NOT EXISTS school_hours TEXT;
ALTER TABLE academy_trials
    ADD COLUMN IF NOT EXISTS user_id BIGINT REFERENCES academy_users(id);

UPDATE academy_users
SET child_birth_year = EXTRACT(YEAR FROM child_birth_date)::INTEGER
WHERE child_birth_year IS NULL AND child_birth_date IS NOT NULL;

UPDATE academy_trials
SET child_birth_year = EXTRACT(YEAR FROM (CURRENT_DATE - make_interval(years => child_age)))::INTEGER
WHERE child_birth_year IS NULL AND child_age IS NOT NULL;

ALTER TABLE academy_users DROP COLUMN IF EXISTS child_age;
ALTER TABLE academy_users DROP COLUMN IF EXISTS child_birth_date;
ALTER TABLE academy_trials DROP COLUMN IF EXISTS child_age;

CREATE UNIQUE INDEX IF NOT EXISTS academy_users_identity_idx
    ON academy_users (parent_phone, child_name, assigned_group_id);
