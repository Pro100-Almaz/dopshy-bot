ALTER TABLE academy_trials
    ADD COLUMN IF NOT EXISTS preferred_date DATE;

ALTER TABLE academy_trials
    ADD COLUMN IF NOT EXISTS preferred_weekday INTEGER;

ALTER TABLE academy_trials
    ADD COLUMN IF NOT EXISTS preferred_time_start TIME;

ALTER TABLE academy_trials
    ADD COLUMN IF NOT EXISTS preferred_time_end TIME;

ALTER TABLE academy_trials
    ADD CONSTRAINT academy_trials_preferred_weekday_check
    CHECK (preferred_weekday IS NULL OR preferred_weekday BETWEEN 0 AND 6);
