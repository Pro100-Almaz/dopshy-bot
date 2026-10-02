-- Subscription-style payment plans for contracts.
--
-- A contract's `price` is split into INSTALLMENTS, each collected on its own
-- due date as a regular ApiPay invoice (or, for a number that is not in Kaspi,
-- a payment link sent on WhatsApp and confirmed by a manager). We schedule the
-- invoices ourselves instead of using ApiPay's /subscriptions: that API bills a
-- fixed amount on a fixed calendar, and the per-booking mode below needs both
-- the date and the amount to follow the bookings.
--
-- Two modes (contracts.payment_mode):
--   static  — N installments on fixed dates, amounts fixed when the plan is made
--   dynamic — one installment per booked slot, due when that slot ends; the
--             amount is worked out at issue time as
--             (price - paid - already invoiced) / installments still to issue,
--             so adding or cancelling bookings re-spreads what is left.
--
-- A contract without a plan keeps payment_mode NULL and behaves exactly as
-- before.

ALTER TABLE contracts ADD COLUMN IF NOT EXISTS payment_mode      TEXT;
ALTER TABLE contracts ADD COLUMN IF NOT EXISTS payment_frequency TEXT;
-- The number the invoices go to. May differ from `phone` (an accountant pays
-- for a company's contract).
ALTER TABLE contracts ADD COLUMN IF NOT EXISTS billing_phone     TEXT;
-- How installments are delivered: 'kaspi_invoice' (ApiPay push, settled by
-- webhook) or 'whatsapp_link' (payment link, settled by a manager).
ALTER TABLE contracts ADD COLUMN IF NOT EXISTS payment_channel   TEXT;
-- Roll-up of the installments, kept in step by contract_billing:
-- none | scheduled | awaiting | overdue | paid | cancelled
ALTER TABLE contracts ADD COLUMN IF NOT EXISTS payment_status    TEXT NOT NULL DEFAULT 'none';

ALTER TABLE contracts DROP CONSTRAINT IF EXISTS contracts_payment_mode_check;
ALTER TABLE contracts ADD CONSTRAINT contracts_payment_mode_check
    CHECK (payment_mode IS NULL OR payment_mode IN ('static', 'dynamic'));

CREATE TABLE IF NOT EXISTS contract_installments (
    id              SERIAL        PRIMARY KEY,
    contract_id     INTEGER       NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    seq             INTEGER       NOT NULL,
    -- Dynamic mode only: the booking (first row of a day-crossing pair) this
    -- installment pays for.
    booking_id      INTEGER       REFERENCES bookings(id) ON DELETE SET NULL,
    due_at          TIMESTAMPTZ   NOT NULL,
    -- NULL until fixed: static plans fix it at creation, dynamic ones at issue.
    amount          NUMERIC(12,2),
    -- Set by a manager; never re-spread by the automatic recalculation.
    amount_locked   BOOLEAN       NOT NULL DEFAULT FALSE,
    -- scheduled → issued → paid, with overdue (retries used up) and cancelled.
    status          TEXT          NOT NULL DEFAULT 'scheduled',
    attempts        INTEGER       NOT NULL DEFAULT 0,
    channel         TEXT,
    -- The apipay_invoices row currently asking for this money (latest attempt).
    apipay_row_id   INTEGER,
    issued_at       TIMESTAMPTZ,
    paid_at         TIMESTAMPTZ,
    paid_amount     NUMERIC(12,2),
    paid_via        TEXT,
    last_error      TEXT,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CHECK (status IN ('scheduled', 'issued', 'paid', 'overdue', 'cancelled')),
    UNIQUE (contract_id, seq)
);

-- The issuing sweep: only what is still waiting to be sent.
CREATE INDEX IF NOT EXISTS idx_contract_installments_due
    ON contract_installments (due_at)
 WHERE status = 'scheduled';
CREATE INDEX IF NOT EXISTS idx_contract_installments_contract
    ON contract_installments (contract_id);
-- One live installment per booking. Partial, so a plan that is replaced can
-- cancel the old installment and create a new one for the same booking.
CREATE UNIQUE INDEX IF NOT EXISTS uq_contract_installments_booking
    ON contract_installments (booking_id)
 WHERE booking_id IS NOT NULL AND status <> 'cancelled';

-- An ApiPay invoice raised for an installment rather than for bookings. Such
-- rows keep booking_ids empty, so the booking-side avans logic (confirm on
-- paid, release on failed, re-issue on cancel) never touches them.
ALTER TABLE apipay_invoices ADD COLUMN IF NOT EXISTS contract_installment_id INTEGER
    REFERENCES contract_installments(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_apipay_invoices_installment
    ON apipay_invoices (contract_installment_id)
 WHERE contract_installment_id IS NOT NULL;
