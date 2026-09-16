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
