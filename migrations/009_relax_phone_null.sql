-- Manager-created bookings (walk-ins / phone bookings) have no WhatsApp sender,
-- so phone must be nullable.

ALTER TABLE bookings ALTER COLUMN phone DROP NOT NULL;
