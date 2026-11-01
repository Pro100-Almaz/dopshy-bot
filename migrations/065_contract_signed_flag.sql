-- Monthly customers are contracts even when no paper contract was signed.
-- The flag records that distinction without changing contract eligibility.
ALTER TABLE contracts
    ADD COLUMN IF NOT EXISTS has_signed_contract BOOLEAN NOT NULL DEFAULT FALSE;
