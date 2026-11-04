-- Refunds go back through ApiPay (Kaspi) instead of only being recorded.
--
-- A refund is a money operation with an asynchronous outcome: POST
-- /invoices/{id}/refund answers "accepted" (pending), and the result arrives
-- later as the `invoice.refunded` webhook (completed | failed) or is read from
-- GET /invoices/{id}/refunds. So each row is a state machine:
--
--   requested   row + Idempotency-Key committed, ApiPay has not acknowledged
--               yet (no answer, 5xx, 429). The sweeper resends with the SAME key
--               and the same body — ApiPay then returns the same refund.
--   processing  ApiPay accepted it (apipay_refund_id known); waiting for result.
--   checking    ApiPay reports the outcome as uncertain (money may have left).
--               Never retried automatically; resolved by a human in ApiPay.
--   refunded    money returned.                                (terminal)
--   failed      ApiPay/Kaspi refused; error_code says why.     (terminal)
--   recorded    a pre-ApiPay manual entry (rows from migration 066).
--
-- Idempotency, in three layers:
--   request_key      one per press of the manager's button: a double click or
--                    a resent HTTP request finds the existing row.
--   idempotency_key  sent to ApiPay; written BEFORE the request.
--   uq_booking_refunds_inflight
--                    at most one unfinished refund per ApiPay invoice — ApiPay
--                    itself refuses a second one (qr_refund_link_invoice_busy).

ALTER TABLE booking_refunds
    ADD COLUMN IF NOT EXISTS method           TEXT        NOT NULL DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS state            TEXT        NOT NULL DEFAULT 'recorded',
    ADD COLUMN IF NOT EXISTS invoice_id       BIGINT,
    ADD COLUMN IF NOT EXISTS apipay_refund_id BIGINT,
    ADD COLUMN IF NOT EXISTS idempotency_key  TEXT,
    ADD COLUMN IF NOT EXISTS request_key      TEXT,
    ADD COLUMN IF NOT EXISTS error_code       TEXT,
    ADD COLUMN IF NOT EXISTS updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE booking_refunds DROP CONSTRAINT IF EXISTS booking_refunds_state_check;
ALTER TABLE booking_refunds ADD CONSTRAINT booking_refunds_state_check
    CHECK (state IN ('recorded', 'requested', 'processing', 'checking', 'refunded', 'failed'));

CREATE UNIQUE INDEX IF NOT EXISTS uq_booking_refunds_apipay_id
    ON booking_refunds (apipay_refund_id) WHERE apipay_refund_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_booking_refunds_idem_key
    ON booking_refunds (idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_booking_refunds_request_key
    ON booking_refunds (request_key) WHERE request_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_booking_refunds_inflight
    ON booking_refunds (invoice_id)
    WHERE state IN ('requested', 'processing', 'checking');
CREATE INDEX IF NOT EXISTS idx_booking_refunds_unfinished
    ON booking_refunds (state) WHERE state IN ('requested', 'processing', 'checking');
