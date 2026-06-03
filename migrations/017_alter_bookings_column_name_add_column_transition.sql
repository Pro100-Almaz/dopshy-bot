-- Add group_uuid UUID string which ties bookings together
-- Instances with same group_uuid should be updated / deleted / created together

ALTER TABLE bookings ADD COLUMN IF NOT EXISTS group_transition UUID;
ALTER TABLE bookings RENAME COLUMN group_uuid TO group_repetition;