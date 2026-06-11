CREATE TABLE IF NOT EXISTS field_prices(
    id SERIAL PRIMARY KEY,
    format_name VARCHAR(10) NOT NULL,
    pricing_type VARCHAR(20) NOT NULL,
    price_per_hour NUMERIC(10, 2) NOT NULL
);

INSERT INTO field_prices (format_name, pricing_type, price_per_hour)
VALUES
    ('5x5', 'morning_day', 22000),
    ('5x5', 'evening', 25000),
    ('5x5', 'late_night', 22000),
    ('5x5', 'after_midnight', 20000),
    ('5x5', 'weekend_holiday', 25000),

    ('6x6', 'morning_day', 27000),
    ('6x6', 'evening', 30000),
    ('6x6', 'late_night', 27000),
    ('6x6', 'after_midnight', 24000),
    ('6x6', 'weekend_holiday', 30000);
