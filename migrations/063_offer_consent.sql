-- Offer (оферта) consent before the ApiPay avans invoice.
--
-- No Kaspi invoice may be raised for a booking until the client has agreed to
-- the public offer (non-refundable avans). The proof lives on the booking row
-- itself, and `apipay_service.queue_invoice` refuses any booking whose
-- `consent_at` is NULL: it is the one place every invoice path goes through.
--
-- New booking state `awaiting_consent`: the slot is HELD (it joins the overlap
-- guard below) while the client is being asked for consent and for a Kaspi
-- number. No invoice exists yet in this state.

-- ── Proof of consent on the booking ─────────────────────────────────────────
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS kaspi_phone          TEXT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS consent_at           TIMESTAMPTZ;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS consent_channel      TEXT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS consent_text_version TEXT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS offer_version        TEXT;
-- How the avans is collected: 'kaspi_invoice' (ApiPay push to kaspi_phone) or
-- 'link' (the payment number had no Kaspi / the check failed: the client got
-- the Kaspi payment link and an admin confirms the payment by its receipt).
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS payment_method       TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'bookings_consent_channel_chk') THEN
        ALTER TABLE bookings ADD CONSTRAINT bookings_consent_channel_chk
            CHECK (consent_channel IS NULL OR consent_channel IN ('bot', 'site', 'admin_bot'));
    END IF;
END $$;

-- ── Last Kaspi number billed successfully, offered again next time ─────────
ALTER TABLE customers ADD COLUMN IF NOT EXISTS kaspi_phone TEXT;

-- ── awaiting_consent holds the slot ─────────────────────────────────────────
-- Same definition as 051 plus the new state.
ALTER TABLE bookings DROP CONSTRAINT IF EXISTS bookings_no_overlap;
ALTER TABLE bookings
    ADD CONSTRAINT bookings_no_overlap
    EXCLUDE USING gist (
        field WITH =,
        tstzrange(start_at, end_at) WITH &&
    )
    WHERE (state IN ('awaiting_consent', 'awaiting_payment', 'confirmed') AND NOT is_test);

-- ── The consent dialog ─────────────────────────────────────────────────────
-- One row per "please agree before we bill you" conversation. It covers every
-- booking that one invoice will pay for (a manager batch can be several), and
-- remembers where the client is in the dialog:
--   confirm      manager-created: waiting for the client to confirm the booking
--                itself (Подтверждаю / Отменить) — the offer comes next
--   offer        waiting for Келісемін / Жоқ
--   same_number  waiting for "bill the same Kaspi number as last time?"
--   new_number   waiting for the client to type a Kaspi number (asked at most
--                KASPI_PHONE_MAX_RETRIES times)
--   link         no number with Kaspi after the retries (or the check failed):
--                admin alerted, payment link sent (bookings awaiting_payment)
--   done | declined | expired | cancelled   closed
CREATE TABLE IF NOT EXISTS consent_requests (
    id               SERIAL        PRIMARY KEY,
    phone            TEXT          NOT NULL,           -- client's WhatsApp number, digits
    booking_ids      INTEGER[]     NOT NULL,           -- every booking held for this consent
    chargeable_ids   INTEGER[]     NOT NULL,           -- the ones the avans invoice pays for
    slot_count       INTEGER       NOT NULL,
    amount           NUMERIC(12,2),                    -- manager override; NULL = slot_count x avans
    description      TEXT,
    source           TEXT          NOT NULL,           -- apipay_invoices.source: bot | manager
    consent_channel  TEXT          NOT NULL CHECK (consent_channel IN ('bot', 'site', 'admin_bot')),
    step             TEXT          NOT NULL DEFAULT 'offer'
                     CHECK (step IN ('confirm', 'offer', 'same_number', 'new_number', 'link',
                                     'done', 'declined', 'expired', 'cancelled')),
    lang             TEXT,
    chat_id          TEXT,
    provider         TEXT,
    kaspi_phone      TEXT,                             -- payment number given up front (manager form)
    suggested_phone  TEXT,                             -- customers.kaspi_phone offered at same_number
    kaspi_retries    INTEGER       NOT NULL DEFAULT 0, -- times the client was asked for another number
    prompt_delivery  TEXT,                             -- interactive | template | failed (admin-created)
    expires_at       TIMESTAMPTZ   NOT NULL,
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_consent_requests_open
    ON consent_requests (phone, id DESC) WHERE step IN ('confirm', 'offer', 'same_number', 'new_number');
CREATE INDEX IF NOT EXISTS idx_consent_requests_bookings
    ON consent_requests USING GIN (booking_ids);

-- ── Last inbound message per WhatsApp number ───────────────────────────────
-- Free-form messages only reach a client within 24h of their last message;
-- past that a Meta-approved template is required. Written on every inbound.
CREATE TABLE IF NOT EXISTS whatsapp_last_inbound (
    phone       TEXT        PRIMARY KEY,
    at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
