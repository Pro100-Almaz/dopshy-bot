-- Registered customers and reusable fixed-amount discounts.

CREATE TABLE IF NOT EXISTS customers (
    id                  BIGSERIAL PRIMARY KEY,
    name                VARCHAR(100),
    phone               VARCHAR(20) NOT NULL UNIQUE,
    is_regular_customer BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (phone ~ '^[0-9]+$')
);

CREATE TABLE IF NOT EXISTS discounts (
    id              BIGSERIAL PRIMARY KEY,
    customer_id     BIGINT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    discount_amount NUMERIC(12,2) NOT NULL CHECK (discount_amount > 0),
    condition       TEXT,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected')),
    usage_limit     INTEGER NOT NULL DEFAULT 5 CHECK (usage_limit > 0),
    usages_left     INTEGER NOT NULL DEFAULT 5 CHECK (usages_left >= 0),
    is_active       BOOLEAN NOT NULL DEFAULT FALSE,
    created_by      TEXT,
    approved_by     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at     TIMESTAMPTZ,
    CHECK (usages_left <= usage_limit),
    CHECK (status = 'approved' OR is_active = FALSE)
);

ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS discount_id BIGINT REFERENCES discounts(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS discount_amount NUMERIC(12,2) NOT NULL DEFAULT 0
        CHECK (discount_amount >= 0),
    ADD COLUMN IF NOT EXISTS price_before_discount NUMERIC(12,2);

-- booking_history also carries customer/discount administration events so the
-- existing history feed remains the single audit stream consumed by the UI.
ALTER TABLE booking_history
    ADD COLUMN IF NOT EXISTS customer_id BIGINT REFERENCES customers(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS discount_id BIGINT REFERENCES discounts(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers(phone);
CREATE INDEX IF NOT EXISTS idx_discounts_customer ON discounts(customer_id);
CREATE INDEX IF NOT EXISTS idx_discounts_available
    ON discounts(customer_id, status, is_active, usages_left);
CREATE INDEX IF NOT EXISTS idx_bookings_discount ON bookings(discount_id);
CREATE INDEX IF NOT EXISTS idx_booking_history_customer ON booking_history(customer_id);
CREATE INDEX IF NOT EXISTS idx_booking_history_discount ON booking_history(discount_id);

-- Normalize existing arena booking phones to the same digits-only key used by
-- customers and discounts. Empty/null legacy values remain untouched.
UPDATE bookings
SET phone = regexp_replace(phone, '[^0-9]', '', 'g')
WHERE phone IS NOT NULL
  AND phone <> ''
  AND phone <> regexp_replace(phone, '[^0-9]', '', 'g');

UPDATE academy_trials
SET phone = regexp_replace(phone, '[^0-9]', '', 'g')
WHERE phone IS NOT NULL
  AND phone <> ''
  AND phone <> regexp_replace(phone, '[^0-9]', '', 'g');

UPDATE contracts
SET phone = regexp_replace(phone, '[^0-9]', '', 'g')
WHERE phone IS NOT NULL
  AND phone <> ''
  AND phone <> regexp_replace(phone, '[^0-9]', '', 'g');
