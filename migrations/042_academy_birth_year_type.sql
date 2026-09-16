ALTER TABLE academy_users
    ALTER COLUMN child_birth_year TYPE INTEGER
    USING child_birth_year::INTEGER;

ALTER TABLE academy_trials
    ALTER COLUMN child_birth_year TYPE INTEGER
    USING child_birth_year::INTEGER;

UPDATE academy_users SET child_birth_year = NULL
WHERE child_birth_year IS NOT NULL AND child_birth_year NOT BETWEEN 1900 AND 2100;
UPDATE academy_trials SET child_birth_year = NULL
WHERE child_birth_year IS NOT NULL AND child_birth_year NOT BETWEEN 1900 AND 2100;

ALTER TABLE academy_users
    ADD CONSTRAINT academy_users_birth_year_check
    CHECK (child_birth_year IS NULL OR child_birth_year BETWEEN 1900 AND 2100);

ALTER TABLE academy_trials
    ADD CONSTRAINT academy_trials_birth_year_check
    CHECK (child_birth_year IS NULL OR child_birth_year BETWEEN 1900 AND 2100);
