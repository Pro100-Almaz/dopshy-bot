-- Bring databases that applied an earlier draft of 063_offer_consent.sql up to
-- its final shape. Idempotent: on a database that ran the final 063 this
-- changes nothing.
--
--   * consent_requests.kaspi_retries — times the client was asked for another
--     Kaspi number (KASPI_PHONE_MAX_RETRIES).
--   * consent_requests.kaspi_phone — payment number given with a manager booking.
--   * step 'confirm' — manager-created bookings are confirmed by the client
--     before the offer is asked.
--   * bookings.payment_method — kaspi_invoice | link.

ALTER TABLE consent_requests ADD COLUMN IF NOT EXISTS kaspi_retries INTEGER NOT NULL DEFAULT 0;
ALTER TABLE consent_requests ADD COLUMN IF NOT EXISTS kaspi_phone   TEXT;
ALTER TABLE consent_requests DROP COLUMN IF EXISTS kaspi_attempts;
ALTER TABLE bookings         ADD COLUMN IF NOT EXISTS payment_method TEXT;

DO $$
DECLARE
    con TEXT;
BEGIN
    FOR con IN
        SELECT conname FROM pg_constraint
         WHERE conrelid = 'consent_requests'::regclass AND contype = 'c'
           AND pg_get_constraintdef(oid) LIKE '%step%'
    LOOP
        EXECUTE format('ALTER TABLE consent_requests DROP CONSTRAINT %I', con);
    END LOOP;
END $$;

-- Rows from the earliest draft may still carry the removed 'admin' step.
UPDATE consent_requests SET step = 'link' WHERE step = 'admin';

ALTER TABLE consent_requests ADD CONSTRAINT consent_requests_step_check
    CHECK (step IN ('confirm', 'offer', 'same_number', 'new_number', 'link',
                    'done', 'declined', 'expired', 'cancelled'));

DROP INDEX IF EXISTS idx_consent_requests_open;
CREATE INDEX idx_consent_requests_open
    ON consent_requests (phone, id DESC) WHERE step IN ('confirm', 'offer', 'same_number', 'new_number');
