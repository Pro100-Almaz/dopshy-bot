-- Add the state-machine columns to bookings. Nullable/defaulted here; tightened
-- in later migrations after backfill.

ALTER TABLE bookings ADD COLUMN IF NOT EXISTS state          VARCHAR(20);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS start_at       TIMESTAMPTZ;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS end_at         TIMESTAMPTZ;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS client_token   UUID NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS reserved_until TIMESTAMPTZ;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS source         VARCHAR(20) NOT NULL DEFAULT 'whatsapp';
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS price_total    NUMERIC(10, 2);
