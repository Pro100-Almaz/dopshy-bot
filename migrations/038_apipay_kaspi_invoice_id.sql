-- Kaspi's own id for the payment, reported on the `invoice.status_changed`
-- webhook as `invoice.kaspi_invoice_id`.
--
-- Refunds are deliberately manual (done from the ApiPay dashboard), and this is
-- the id a manager searches Kaspi by — so it has to be persisted, not just logged.
--
-- Migration 037 already declares this column for fresh databases; this file
-- catches the ones that applied 037 before it did. IF NOT EXISTS makes both
-- paths converge.

ALTER TABLE apipay_invoices ADD COLUMN IF NOT EXISTS kaspi_invoice_id TEXT;
