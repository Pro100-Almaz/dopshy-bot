-- Acceptable payment recipients. A receipt is accepted only if its extracted
-- bank + identifier matches an active row here.
--   Kaspi  → match on seller BIN (and/or name contains the stored name)
--   Halyk  → match on recipient phone (and/or recipient name)

CREATE TABLE IF NOT EXISTS payment_recipients (
    id     SERIAL      PRIMARY KEY,
    bank   VARCHAR(20) NOT NULL,
    bin    VARCHAR(20),
    name   TEXT,
    phone  VARCHAR(20),
    active BOOLEAN     NOT NULL DEFAULT TRUE
);

INSERT INTO payment_recipients (bank, bin, name, phone)
SELECT 'kaspi', '870203301478', 'DOPSHY', NULL
WHERE NOT EXISTS (
    SELECT 1 FROM payment_recipients WHERE bank = 'kaspi' AND bin = '870203301478'
);

INSERT INTO payment_recipients (bank, bin, name, phone)
SELECT 'halyk', NULL, 'Мухтар', '77029721819'
WHERE NOT EXISTS (
    SELECT 1 FROM payment_recipients WHERE bank = 'halyk' AND phone = '77029721819'
);
