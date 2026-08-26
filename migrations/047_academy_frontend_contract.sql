-- Academy frontend contract fields and source-of-truth tables.

ALTER TABLE academy_groups
    ADD COLUMN IF NOT EXISTS age_min INTEGER;
ALTER TABLE academy_groups
    ADD COLUMN IF NOT EXISTS age_max INTEGER;
ALTER TABLE academy_groups
    ADD COLUMN IF NOT EXISTS shift TEXT;

ALTER TABLE academy_groups
    DROP CONSTRAINT IF EXISTS academy_groups_age_range_check;
ALTER TABLE academy_groups
    ADD CONSTRAINT academy_groups_age_range_check
    CHECK (
        (age_min IS NULL OR age_min >= 0)
        AND (age_max IS NULL OR age_max >= 0)
        AND (age_min IS NULL OR age_max IS NULL OR age_min <= age_max)
    );

ALTER TABLE academy_trials
    ADD COLUMN IF NOT EXISTS attendance_state TEXT NOT NULL DEFAULT 'pending';

UPDATE academy_trials
SET attendance_state = 'attended'
WHERE attended IS TRUE
  AND attendance_state = 'pending';

ALTER TABLE academy_trials
    DROP CONSTRAINT IF EXISTS academy_trials_attendance_state_check;
ALTER TABLE academy_trials
    ADD CONSTRAINT academy_trials_attendance_state_check
    CHECK (attendance_state IN ('pending', 'attended', 'missed'));

CREATE TABLE IF NOT EXISTS academy_payments (
    id BIGSERIAL PRIMARY KEY,
    group_type VARCHAR(20) NOT NULL CHECK (group_type IN ('football', 'boxing')),
    child_name TEXT,
    parent_phone TEXT,
    discount TEXT,
    amount NUMERIC(12, 2),
    sender_bank TEXT,
    receiver_bank TEXT,
    check_number TEXT,
    payment_date TIMESTAMPTZ,
    due_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    check_status TEXT NOT NULL DEFAULT 'manual',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    confirmed BOOLEAN,
    notes TEXT,
    check_url TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_academy_payments_group_type
    ON academy_payments (group_type);
CREATE INDEX IF NOT EXISTS idx_academy_payments_due_active
    ON academy_payments (due_date, is_active);

CREATE TABLE IF NOT EXISTS academy_bot_content (
    group_type VARCHAR(20) PRIMARY KEY CHECK (group_type IN ('football', 'boxing')),
    content JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
