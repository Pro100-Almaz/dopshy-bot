-- ApiPay.kz (Kaspi Pay) invoices.
--
-- One invoice per POST /api/manager/bookings/batch request: a single total
-- avans for the whole batch, never one invoice per booking. `booking_ids`
-- lists exactly the bookings that invoice pays for (non-repeating slots only —
-- repeating slots carry no avans), so a `paid` webhook flips those bookings
-- awaiting_payment -> confirmed and leaves the rest alone.
--
-- `invoice_id` is the id ApiPay assigns; it is NULL only for the brief moment
-- between our INSERT and the API response (both happen in one transaction, so
-- a NULL row never survives a commit).

CREATE TABLE IF NOT EXISTS apipay_invoices (
    id                SERIAL        PRIMARY KEY,
    invoice_id        BIGINT        UNIQUE,
    external_order_id TEXT          NOT NULL UNIQUE,
    phone             TEXT          NOT NULL,
    amount            NUMERIC(12,2) NOT NULL,
    status            TEXT          NOT NULL DEFAULT 'processing',
    booking_ids       INTEGER[]     NOT NULL DEFAULT '{}',
    -- Kaspi's own id for the payment, reported on the webhook. Refunds are
    -- manual (ApiPay dashboard), and this is what a manager searches Kaspi by.
    kaspi_invoice_id  TEXT,
    error_code        TEXT,
    error_message     TEXT,
    paid_at           TIMESTAMPTZ,
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_apipay_invoices_status   ON apipay_invoices (status);
CREATE INDEX IF NOT EXISTS idx_apipay_invoices_bookings ON apipay_invoices USING GIN (booking_ids);
