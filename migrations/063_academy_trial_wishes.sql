-- What the client wishes for in a class, beyond preferred_date and the
-- preferred_time_start..preferred_time_end range: several weekdays
-- (0 = Monday) and the trainers named. The class list is filtered by them.

ALTER TABLE academy_trials
    ADD COLUMN IF NOT EXISTS preferred_weekdays INTEGER[];

ALTER TABLE academy_trials
    ADD COLUMN IF NOT EXISTS preferred_trainers TEXT[];
