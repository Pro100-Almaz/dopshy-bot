-- 054 switched the tariff model but left the prices in seeds/field_prices.sql,
-- which is not run on deploy. Without this, existing DBs keep the old prices
-- and have no weekday_special rows, so those windows price at 0.
-- Keep in sync with seeds/field_prices.sql.

INSERT INTO field_prices (format_name, pricing_type, price_per_hour)
VALUES
    ('5x5', 'morning_day', 28000),
    ('5x5', 'evening', 33000),
    ('5x5', 'late_night', 28000),
    ('5x5', 'after_midnight', 23000),
    ('5x5', 'weekday_special', 20000),
    ('6x6', 'morning_day', 33000),
    ('6x6', 'evening', 38000),
    ('6x6', 'late_night', 33000),
    ('6x6', 'after_midnight', 28000),
    ('6x6', 'weekday_special', 24000)
ON CONFLICT (format_name, pricing_type)
    DO UPDATE SET price_per_hour = EXCLUDED.price_per_hour;
