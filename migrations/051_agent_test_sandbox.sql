-- Agent-test console sandbox.
--
-- Conversations driven from the admin test console run the real pipeline, so
-- they create real draft/confirmed rows. `is_test` marks those rows so they are
-- invisible to availability, Google Sheets, notifications and the slot lock,
-- while still exercising the genuine state machine.

ALTER TABLE bookings       ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE academy_trials ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE academy_users  ADD COLUMN IF NOT EXISTS is_test BOOLEAN NOT NULL DEFAULT FALSE;

-- Recreate the overlap guard so a test booking can never reserve a slot away
-- from a real customer. Same definition as 005 plus `AND NOT is_test`.
ALTER TABLE bookings DROP CONSTRAINT IF EXISTS bookings_no_overlap;
ALTER TABLE bookings
    ADD CONSTRAINT bookings_no_overlap
    EXCLUDE USING gist (
        field WITH =,
        tstzrange(start_at, end_at) WITH &&
    )
    WHERE (state IN ('awaiting_payment', 'confirmed') AND NOT is_test);

-- Partial index: test rows are a tiny minority, so only they need indexing.
CREATE INDEX IF NOT EXISTS bookings_is_test_idx       ON bookings       (is_test) WHERE is_test;
CREATE INDEX IF NOT EXISTS academy_trials_is_test_idx ON academy_trials (is_test) WHERE is_test;

-- One row per console conversation. `test_phone` is the synthetic sender the
-- pipeline sees, and is what ties the session to its history and test rows.
CREATE TABLE IF NOT EXISTS agent_test_sessions (
    id              BIGSERIAL PRIMARY KEY,
    bot_name        TEXT NOT NULL,
    phone_number_id TEXT NOT NULL,
    test_phone      TEXT NOT NULL UNIQUE,
    title           TEXT,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Synthetic phones are allocated from this sequence and never recycled, so a
-- deleted session's residue can never be picked up by a new one.
CREATE SEQUENCE IF NOT EXISTS console_phone_seq START 1;

CREATE INDEX IF NOT EXISTS agent_test_sessions_created_at_idx
    ON agent_test_sessions (created_at DESC);
