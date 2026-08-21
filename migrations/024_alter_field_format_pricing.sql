CREATE TABLE IF NOT EXISTS field_prices(
    id SERIAL PRIMARY KEY,
    format_name VARCHAR(10) NOT NULL,
    pricing_type VARCHAR(20) NOT NULL,
    price_per_hour NUMERIC(10, 2) NOT NULL
);
