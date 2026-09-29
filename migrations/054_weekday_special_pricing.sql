-- Replace the weekend/holiday tariff with a weekday special tariff.
--
-- There is no separate weekend price any more: Saturday and Sunday use the
-- ordinary time-of-day periods. Instead, Monday–Friday 12:00–16:00 and
-- 18:30–20:00 are charged at `weekday_special`, cut out of morning_day and
-- evening respectively. Mirrors handlers/payment/pricing.py
-- (PRICING_PERIODS / WEEKDAY_PRICING_PERIODS).
--
-- field_prices had no uniqueness, so re-running seeds/field_prices.sql
-- (ON CONFLICT DO NOTHING never fired) appended duplicate rows, and the trigger
-- below would sum a duplicated period twice. Keep the newest row per
-- (format_name, pricing_type) and enforce it from now on; the seed then upserts.

DELETE FROM field_prices WHERE pricing_type = 'weekend_holiday';

DELETE FROM field_prices fp
USING field_prices newer
WHERE newer.format_name = fp.format_name
  AND newer.pricing_type = fp.pricing_type
  AND newer.id > fp.id;

CREATE UNIQUE INDEX IF NOT EXISTS field_prices_format_type_uniq
    ON field_prices (format_name, pricing_type);

CREATE OR REPLACE FUNCTION bookings_compute_fields() RETURNS trigger AS $$
DECLARE
    _fmt TEXT;
    _start_min INT;
    _end_min INT;
    _overlap INT;
    _is_weekday BOOLEAN;
    _total NUMERIC(10,2) := 0;
    _period RECORD;
BEGIN
    IF NEW.date IS NOT NULL AND NEW.time_start IS NOT NULL THEN
        NEW.start_at := (NEW.date + NEW.time_start) AT TIME ZONE 'Asia/Almaty';
    END IF;
    IF NEW.date IS NOT NULL AND NEW.time_end IS NOT NULL THEN
        NEW.end_at := (NEW.date + NEW.time_end) AT TIME ZONE 'Asia/Almaty';
    END IF;

    IF NEW.price_total IS NULL
        AND NEW.field IS NOT NULL
        AND NEW.time_start IS NOT NULL
        AND NEW.time_end IS NOT NULL THEN

        SELECT format INTO _fmt FROM fields WHERE id = NEW.field;

        _start_min := EXTRACT(HOUR FROM NEW.time_start)::INT * 60
                    + EXTRACT(MINUTE FROM NEW.time_start)::INT;

        _end_min := EXTRACT(HOUR FROM NEW.time_end)::INT * 60
                  + EXTRACT(MINUTE FROM NEW.time_end)::INT;
        -- Treat any 23:59 (regardless of seconds) as end-of-day midnight (1440),
        -- matching Python's _to_minutes().
        IF EXTRACT(HOUR FROM NEW.time_end) = 23
           AND EXTRACT(MINUTE FROM NEW.time_end) = 59 THEN
            _end_min := 1440;
        END IF;

        IF _end_min <= _start_min THEN
            _end_min := 1440;
        END IF;

        -- ISODOW: 1 = Monday … 7 = Sunday.
        _is_weekday := COALESCE(EXTRACT(ISODOW FROM NEW.date) BETWEEN 1 AND 5, FALSE);

        -- Walk through each pricing period and accumulate cost. Each row says
        -- whether it applies on weekdays, on weekends, or on both.
        FOR _period IN
            SELECT fp.price_per_hour, p.p_start, p.p_end
            FROM (VALUES
                ('after_midnight',     0,  420, TRUE,  TRUE),
                ('morning_day',      420,  720, TRUE,  TRUE),
                ('weekday_special',  720,  960, TRUE,  FALSE),
                ('morning_day',      720,  960, FALSE, TRUE),
                ('morning_day',      960, 1110, TRUE,  TRUE),
                ('weekday_special', 1110, 1200, TRUE,  FALSE),
                ('evening',         1110, 1200, FALSE, TRUE),
                ('evening',         1200, 1320, TRUE,  TRUE),
                ('late_night',      1320, 1440, TRUE,  TRUE)
            ) AS p(pricing_type, p_start, p_end, on_weekday, on_weekend)
            JOIN field_prices fp
              ON fp.pricing_type = p.pricing_type
             AND fp.format_name = _fmt
            WHERE CASE WHEN _is_weekday THEN p.on_weekday ELSE p.on_weekend END
        LOOP
            _overlap := GREATEST(0,
                LEAST(_end_min, _period.p_end) - GREATEST(_start_min, _period.p_start));
            IF _overlap > 0 THEN
                _total := _total + (_overlap / 60.0) * _period.price_per_hour;
            END IF;
        END LOOP;

        NEW.price_total := ROUND(_total, 2);
    END IF;

    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
