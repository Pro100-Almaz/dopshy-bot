CREATE TABLE IF NOT EXISTS contracts (
    id            SERIAL PRIMARY KEY,
    customer_name VARCHAR(100) NOT NULL,
    phone         VARCHAR(20),
    start_date    DATE NOT NULL,
    end_date      DATE NOT NULL,
    price         NUMERIC(12,2) NOT NULL,
    status        VARCHAR(20) NOT NULL DEFAULT 'confirmed',
    notes         TEXT,
    source        VARCHAR(100),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (end_date >= start_date)
);

CREATE TABLE IF NOT EXISTS contract_bookings (
    contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    booking_id  INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (contract_id, booking_id),
    UNIQUE (booking_id)
);

CREATE INDEX IF NOT EXISTS idx_contracts_status ON contracts (status);
CREATE INDEX IF NOT EXISTS idx_contracts_dates ON contracts (start_date, end_date);
CREATE INDEX IF NOT EXISTS idx_contract_bookings_contract ON contract_bookings (contract_id);
