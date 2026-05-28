-- DRAFT rows are created at booking-flow start, before the user has picked a
-- date / time / field, so these columns must be nullable. They are validated
-- as present when the booking transitions to awaiting_payment.

ALTER TABLE bookings ALTER COLUMN date       DROP NOT NULL;
ALTER TABLE bookings ALTER COLUMN time_start DROP NOT NULL;
ALTER TABLE bookings ALTER COLUMN time_end   DROP NOT NULL;
ALTER TABLE bookings ALTER COLUMN field      DROP NOT NULL;
ALTER TABLE bookings ALTER COLUMN format     DROP NOT NULL;
