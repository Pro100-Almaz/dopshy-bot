-- Days the academy does not train (holidays, cancellations), filled in by the
-- admin. A trial is never offered on one: the class moves to the next week.
-- The bot also tells a client asking about that day that there is no class.
-- group_type NULL closes the day for both football and boxing.

CREATE TABLE IF NOT EXISTS academy_closed_dates (
    id          SERIAL PRIMARY KEY,
    closed_on   DATE NOT NULL,
    group_type  TEXT CHECK (group_type IN ('football', 'boxing')),
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS academy_closed_dates_day_type
    ON academy_closed_dates (closed_on, COALESCE(group_type, ''));
