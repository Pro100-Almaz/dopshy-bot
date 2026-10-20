-- Answers of an abandoned trial signup (no message for TRIAL_DRAFT_KEEP_SECONDS).
-- The draft itself is cancelled; its answers stay here, with no expiry, so a
-- returning client isn't asked the same questions again. One row per phone and
-- academy (group_type). An explicit cancel or decline deletes the row.

CREATE TABLE IF NOT EXISTS academy_trial_summaries (
    phone            TEXT NOT NULL,
    group_type       TEXT NOT NULL,
    audience         TEXT,
    child_name       VARCHAR(30),
    child_birth_year INTEGER,
    experience       TEXT,
    school_shift     TEXT,
    saved_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (phone, group_type)
);
