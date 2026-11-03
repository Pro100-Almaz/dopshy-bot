-- Refunds and the 48-hour refund window.
--
-- The law gives the client the right to a refund within 48 hours of paying, so
-- the manager UI has to know WHEN the booking was first paid and how much has
-- already been returned.
--
--   bookings.first_paid_at  the moment any money first landed on the booking.
--                           Set once, never moved: later payments (cash at the
--                           arena, a second receipt) do not restart the clock.
--   booking_refunds         one row per refund a manager records. Refunds are
--                           recorded, not executed — the money itself is
--                           returned by hand (ApiPay dashboard, cash, Kaspi).
--
-- first_paid_at is maintained by triggers so every writer (bot receipts,
-- ApiPay webhook, manager edits, manual SQL) stamps it without remembering to.

ALTER TABLE bookings ADD COLUMN IF NOT EXISTS first_paid_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS booking_refunds (
    id               SERIAL        PRIMARY KEY,
    booking_id       INTEGER       NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    amount           NUMERIC(12,2) NOT NULL CHECK (amount > 0),
    comment          TEXT,
    -- Was the refund requested inside the 48h window? Frozen at write time so
    -- the audit trail does not depend on later edits of first_paid_at.
    within_window    BOOLEAN,
    created_by       TEXT          NOT NULL,
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_booking_refunds_booking ON booking_refunds (booking_id);

-- Manual buckets (paid_kaspi_qr / paid_cash / paid_avans) are plain numbers a
-- manager edits in place, so the first edit that RAISES them is the payment.
-- UPDATE only, on purpose: at INSERT paid_avans holds the avans the booking
-- EXPECTS (the prepayment it was created with), not money received.
CREATE OR REPLACE FUNCTION bookings_stamp_first_paid() RETURNS trigger AS $$
BEGIN
    IF NEW.first_paid_at IS NULL
        AND COALESCE(NEW.paid_kaspi_qr, 0) + COALESCE(NEW.paid_cash, 0)
            + COALESCE(NEW.paid_avans, 0)
          > COALESCE(OLD.paid_kaspi_qr, 0) + COALESCE(OLD.paid_cash, 0)
            + COALESCE(OLD.paid_avans, 0) THEN
        NEW.first_paid_at := NOW();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS bookings_stamp_first_paid_trg ON bookings;
CREATE TRIGGER bookings_stamp_first_paid_trg
    BEFORE UPDATE OF paid_kaspi_qr, paid_cash, paid_avans ON bookings
    FOR EACH ROW EXECUTE FUNCTION bookings_stamp_first_paid();

-- Bot payments (WhatsApp receipts, ApiPay avans) are rows in `payments`.
CREATE OR REPLACE FUNCTION payments_stamp_first_paid() RETURNS trigger AS $$
BEGIN
    IF NEW.status = 'accepted' AND NEW.booking_id IS NOT NULL THEN
        UPDATE bookings
           SET first_paid_at = COALESCE(NEW.verified_at, NEW.created_at, NOW())
         WHERE id = NEW.booking_id AND first_paid_at IS NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS payments_stamp_first_paid_trg ON payments;
CREATE TRIGGER payments_stamp_first_paid_trg
    AFTER INSERT OR UPDATE OF status ON payments
    FOR EACH ROW EXECUTE FUNCTION payments_stamp_first_paid();

-- Backfill from what is known. Bookings paid only through the manual buckets
-- before this migration have no recorded payment time and stay NULL — the UI
-- shows "время оплаты неизвестно" for them instead of guessing.
UPDATE bookings b
   SET first_paid_at = src.paid_at
  FROM (
        SELECT booking_id, MIN(COALESCE(verified_at, created_at)) AS paid_at
          FROM payments
         WHERE status = 'accepted' AND booking_id IS NOT NULL
         GROUP BY booking_id
       ) src
 WHERE b.id = src.booking_id AND b.first_paid_at IS NULL;

UPDATE bookings b
   SET first_paid_at = src.paid_at
  FROM (
        SELECT unnest(booking_ids) AS booking_id, MIN(paid_at) AS paid_at
          FROM apipay_invoices
         WHERE paid_at IS NOT NULL
         GROUP BY 1
       ) src
 WHERE b.id = src.booking_id
   AND (b.first_paid_at IS NULL OR src.paid_at < b.first_paid_at);

-- Manual payments entered before this migration left a booking_history row
-- («Изменен сумма …», manager_*_change templates) — its time is the best record
-- of when that money came in.
UPDATE bookings b
   SET first_paid_at = src.paid_at
  FROM (
        SELECT booking_id, MIN(created_at) AS paid_at
          FROM booking_history
         WHERE description LIKE 'Изменен сумма%' AND booking_id IS NOT NULL
         GROUP BY booking_id
       ) src
 WHERE b.id = src.booking_id
   AND COALESCE(b.paid_kaspi_qr, 0) + COALESCE(b.paid_cash, 0) + COALESCE(b.paid_avans, 0) > 0
   AND (b.first_paid_at IS NULL OR src.paid_at < b.first_paid_at);
