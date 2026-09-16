-- History of booking changes.
--
-- One row per human-readable change event. `description` is a rendered string
-- (see integrations/repo/history_descriptions.json for the dynamic templates)
-- and `source` is either a bot source ('whatsapp', 'chatbot:Бот', or
-- 'bot:<integration>' — ApiPay writes 'bot:ApiPay') or the manager's email
-- address (manager-driven change).

CREATE TABLE IF NOT EXISTS booking_history (
    id          BIGSERIAL   PRIMARY KEY,
    booking_id  INTEGER     REFERENCES bookings(id) ON DELETE CASCADE,
    source      TEXT        NOT NULL,
    description TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_booking_history_booking    ON booking_history (booking_id);
CREATE INDEX IF NOT EXISTS idx_booking_history_created_at ON booking_history (created_at);
CREATE INDEX IF NOT EXISTS idx_booking_history_source     ON booking_history (source);
