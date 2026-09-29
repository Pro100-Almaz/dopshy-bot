-- Every booking belongs to a customer. Existing phone-only booking customers
-- are registered in the customers table before the NOT NULL constraint is set.

ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS customer_id BIGINT
        REFERENCES customers(id) ON DELETE RESTRICT;

ALTER TABLE bookings
    DROP CONSTRAINT IF EXISTS bookings_customer_id_fkey;
ALTER TABLE bookings
    ADD CONSTRAINT bookings_customer_id_fkey
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE RESTRICT;

INSERT INTO customers (name, phone, is_regular_customer)
SELECT
    MAX(NULLIF(b.customer_name, '')) AS name,
    regexp_replace(b.phone, '[^0-9]', '', 'g') AS phone,
    FALSE
FROM bookings b
WHERE b.phone IS NOT NULL
  AND regexp_replace(b.phone, '[^0-9]', '', 'g') <> ''
GROUP BY regexp_replace(b.phone, '[^0-9]', '', 'g')
ON CONFLICT (phone) DO UPDATE SET
    name = COALESCE(customers.name, EXCLUDED.name),
    updated_at = NOW();

-- Attach existing bookings wherever a registered customer has the same
-- normalized phone number.
UPDATE bookings b
SET customer_id = c.id
FROM customers c
WHERE b.customer_id IS NULL
  AND b.phone IS NOT NULL
  AND regexp_replace(b.phone, '[^0-9]', '', 'g') = c.phone;

CREATE INDEX IF NOT EXISTS idx_bookings_customer ON bookings(customer_id);

-- Do not silently manufacture fake clients for legacy rows that have no phone.
-- Such rows must be repaired explicitly before this migration can complete.
DO $$
DECLARE
    orphan_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO orphan_count
    FROM bookings
    WHERE customer_id IS NULL;

    IF orphan_count > 0 THEN
        RAISE EXCEPTION
            'Cannot make bookings.customer_id NOT NULL: % booking rows have no resolvable customer phone',
            orphan_count;
    END IF;
END
$$;

ALTER TABLE bookings
    ALTER COLUMN customer_id SET NOT NULL;

-- Covers booking writes outside the manager service (for example chatbot
-- drafts): a known phone automatically receives its registered customer id.
CREATE OR REPLACE FUNCTION set_booking_customer_from_phone()
RETURNS TRIGGER AS $$
DECLARE
    normalized_phone TEXT;
    linked_phone TEXT;
BEGIN
    normalized_phone := regexp_replace(COALESCE(NEW.phone, ''), '[^0-9]', '', 'g');

    IF NEW.customer_id IS NOT NULL THEN
        SELECT phone INTO linked_phone FROM customers WHERE id = NEW.customer_id;
        IF linked_phone IS NULL THEN
            RAISE EXCEPTION 'Customer % does not exist', NEW.customer_id;
        END IF;
        IF normalized_phone = '' THEN
            NEW.phone := linked_phone;
        ELSIF normalized_phone <> linked_phone THEN
            RAISE EXCEPTION 'Booking phone does not match customer %', NEW.customer_id;
        ELSE
            NEW.phone := normalized_phone;
        END IF;
    ELSIF normalized_phone <> '' THEN
        INSERT INTO customers (name, phone, is_regular_customer)
        VALUES (NULLIF(NEW.customer_name, ''), normalized_phone, FALSE)
        ON CONFLICT (phone) DO UPDATE SET
            name = COALESCE(customers.name, EXCLUDED.name),
            updated_at = NOW()
        RETURNING id INTO NEW.customer_id;
        NEW.phone := normalized_phone;
    ELSE
        RAISE EXCEPTION 'Every booking requires customer_id or phone';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS bookings_set_customer_from_phone ON bookings;
CREATE TRIGGER bookings_set_customer_from_phone
BEFORE INSERT OR UPDATE OF phone, customer_id ON bookings
FOR EACH ROW EXECUTE FUNCTION set_booking_customer_from_phone();
