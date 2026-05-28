-- Tighten the state column and enforce client_token uniqueness now that every
-- row has values.

ALTER TABLE bookings ALTER COLUMN state SET DEFAULT 'draft';
ALTER TABLE bookings ALTER COLUMN state SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_bookings_client_token ON bookings (client_token);

CREATE INDEX IF NOT EXISTS idx_bookings_state          ON bookings (state);
CREATE INDEX IF NOT EXISTS idx_bookings_reserved_until ON bookings (reserved_until);
