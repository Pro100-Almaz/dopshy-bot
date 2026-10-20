-- A child who goes to kindergarten has no school shift. The bot records that
-- as school_shift = 'kindergarten' so it stops asking for a shift; classes
-- are then not filtered by school hours.

ALTER TABLE academy_users DROP CONSTRAINT IF EXISTS academy_users_school_shift_check;
ALTER TABLE academy_users
    ADD CONSTRAINT academy_users_school_shift_check
    CHECK (school_shift IS NULL OR school_shift IN ('morning', 'afternoon', 'kindergarten'));

ALTER TABLE academy_trials DROP CONSTRAINT IF EXISTS academy_trials_school_shift_check;
ALTER TABLE academy_trials
    ADD CONSTRAINT academy_trials_school_shift_check
    CHECK (school_shift IS NULL OR school_shift IN ('morning', 'afternoon', 'kindergarten'));
