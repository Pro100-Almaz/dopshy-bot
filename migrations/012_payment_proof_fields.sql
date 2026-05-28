-- Payment proof details + receipt dedup.

ALTER TABLE payments ADD COLUMN IF NOT EXISTS transaction_ref TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS bank            VARCHAR(20);
ALTER TABLE payments ADD COLUMN IF NOT EXISTS receipt_date    TIMESTAMPTZ;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS status          VARCHAR(20);
ALTER TABLE payments ADD COLUMN IF NOT EXISTS reject_reason   TEXT;

-- Same receipt can never be used twice (only enforced when a ref was extracted).
CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_txn_ref
    ON payments (transaction_ref) WHERE transaction_ref IS NOT NULL;
