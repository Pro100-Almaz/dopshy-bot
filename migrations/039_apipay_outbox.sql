-- ApiPay invoices become a transactional outbox.
--
-- Before: the row was INSERTed and `POST /invoices` called inside the bookings
-- transaction. A lost response (timeout after ApiPay had already created the
-- invoice) rolled the row back, so the paid-webhook arrived for an invoice we
-- had no record of — money taken, no booking, one WARNING line.
--
-- After: the row is committed WITH the bookings (status 'created', invoice_id
-- NULL), and sending is a separate, retryable step. `external_order_id` is
-- generated before the call and never regenerated, so it — not `invoice_id` —
-- is the key the webhook resolves against.
--
-- 'created' means "committed, not yet sent"; 'processing'/'pending' keep their
-- ApiPay meaning of "sent, awaiting the client".

ALTER TABLE apipay_invoices ADD COLUMN IF NOT EXISTS send_attempts   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE apipay_invoices ADD COLUMN IF NOT EXISTS last_send_error TEXT;

-- Who to tell when the invoice is paid. NULL for manager-created invoices:
-- those are reported in the manager UI, not pushed to the client's WhatsApp.
ALTER TABLE apipay_invoices ADD COLUMN IF NOT EXISTS notify_chat_id  TEXT;
ALTER TABLE apipay_invoices ADD COLUMN IF NOT EXISTS notify_provider TEXT;
ALTER TABLE apipay_invoices ADD COLUMN IF NOT EXISTS notify_lang     TEXT;
ALTER TABLE apipay_invoices ADD COLUMN IF NOT EXISTS source          TEXT NOT NULL DEFAULT 'manager';

ALTER TABLE apipay_invoices ALTER COLUMN status SET DEFAULT 'created';

-- The outbox scan: unsent rows only. Partial, so it stays tiny however large
-- the table grows — sent invoices drop out of the index entirely.
CREATE INDEX IF NOT EXISTS idx_apipay_invoices_outbox
    ON apipay_invoices (created_at)
 WHERE invoice_id IS NULL AND status = 'created';

-- Rows written before this migration were only ever committed after a
-- successful send, so none of them belong in the outbox. No backfill needed.
