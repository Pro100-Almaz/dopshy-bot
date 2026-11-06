-- Admin-created bookings no longer ask the client for offer consent on
-- WhatsApp: the admin takes the booking on the client's behalf, the Kaspi
-- invoice goes out at once and the client's "booking created" message states
-- that booking means accepting the offer. Those bookings record
-- consent_channel = 'admin'.

ALTER TABLE bookings DROP CONSTRAINT IF EXISTS bookings_consent_channel_chk;
ALTER TABLE bookings ADD CONSTRAINT bookings_consent_channel_chk
    CHECK (consent_channel IS NULL OR consent_channel IN ('bot', 'site', 'admin_bot', 'admin'));
