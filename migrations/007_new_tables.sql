-- Reference + audit + payment tables.

CREATE TABLE IF NOT EXISTS fields (
    id       SMALLINT     PRIMARY KEY,
    name     TEXT         NOT NULL,
    format   VARCHAR(5)   NOT NULL,
    capacity SMALLINT,
    active    BOOLEAN     NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS booking_events (
    id         BIGSERIAL   PRIMARY KEY,
    booking_id INTEGER     REFERENCES bookings(id) ON DELETE CASCADE,
    event      VARCHAR(40) NOT NULL,
    actor_type VARCHAR(20) NOT NULL,
    actor_id   TEXT,
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_booking_events_booking ON booking_events (booking_id);

CREATE TABLE IF NOT EXISTS payments (
    id             SERIAL        PRIMARY KEY,
    booking_id     INTEGER       REFERENCES bookings(id) ON DELETE CASCADE,
    method         VARCHAR(20),
    proof_media_id TEXT,
    verified_by    TEXT,
    verified_at    TIMESTAMPTZ,
    amount         NUMERIC(10, 2),
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_payments_booking ON payments (booking_id);
