-- Baseline schema (idempotent). Mirrors the original init_schema() so a fresh
-- database converges with an existing one before the state-machine migrations run.

CREATE TABLE IF NOT EXISTS bookings (
    id            SERIAL PRIMARY KEY,
    phone         VARCHAR(20)   NOT NULL,
    customer_name VARCHAR(100),
    date          DATE          NOT NULL,
    time_start    TIME          NOT NULL,
    time_end      TIME          NOT NULL,
    field         SMALLINT      NOT NULL,
    format        VARCHAR(5)    NOT NULL,
    players       SMALLINT,
    status        VARCHAR(20)   NOT NULL DEFAULT 'awaiting_payment',
    sheet_row     INTEGER,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    notes         TEXT,
    UNIQUE (date, time_start, field)
);

CREATE INDEX IF NOT EXISTS idx_bookings_phone  ON bookings (phone);
CREATE INDEX IF NOT EXISTS idx_bookings_date   ON bookings (date);
CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings (status);

CREATE TABLE IF NOT EXISTS booking_sessions (
    chat_id    TEXT        PRIMARY KEY,
    state      VARCHAR(20) NOT NULL,
    params     JSONB       NOT NULL DEFAULT '{}',
    booking_id INTEGER     REFERENCES bookings(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS sheets_sync_state (
    id         SERIAL      PRIMARY KEY,
    week_start DATE        NOT NULL UNIQUE,
    synced_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
