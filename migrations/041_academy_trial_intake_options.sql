ALTER TABLE academy_users
    RENAME COLUMN school_hours TO school_shift;

ALTER TABLE academy_trials
    RENAME COLUMN school_hours TO school_shift;

ALTER TABLE academy_users
    ADD CONSTRAINT academy_users_experience_check
    CHECK (experience IS NULL OR experience IN ('Beginner', 'Intermediate', 'Advanced'));

ALTER TABLE academy_users
    ADD CONSTRAINT academy_users_school_shift_check
    CHECK (school_shift IS NULL OR school_shift IN ('morning', 'afternoon'));

ALTER TABLE academy_trials
    ADD CONSTRAINT academy_trials_experience_check
    CHECK (experience IS NULL OR experience IN ('Beginner', 'Intermediate', 'Advanced'));

ALTER TABLE academy_trials
    ADD CONSTRAINT academy_trials_school_shift_check
    CHECK (school_shift IS NULL OR school_shift IN ('morning', 'afternoon'));
