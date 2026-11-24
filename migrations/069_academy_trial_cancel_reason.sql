-- Why an academy trial was cancelled (e.g. 'no_fitting_group' when the bot
-- handed the client to the admin). Bookings keep this in booking_events;
-- academy trials had nowhere to store it.

ALTER TABLE academy_trials ADD COLUMN IF NOT EXISTS cancel_reason TEXT;
