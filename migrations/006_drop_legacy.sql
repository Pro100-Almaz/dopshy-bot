-- Drop the legacy exact-slot unique constraint (superseded by bookings_no_overlap)
-- and the legacy status column (superseded by state).

ALTER TABLE bookings DROP CONSTRAINT IF EXISTS bookings_date_time_start_field_key;
DROP INDEX IF EXISTS idx_bookings_status;
ALTER TABLE bookings DROP COLUMN IF EXISTS status;
