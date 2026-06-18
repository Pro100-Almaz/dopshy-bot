-- Update the bookings_compute_fields trigger to use field_prices (time-of-day
-- pricing) instead of the flat fields.price_per_hour.
--
-- The application layer now always provides price_total, so this trigger is a
-- safety net for manual SQL inserts and legacy paths.
--
-- Weekend/holiday detection is NOT handled here — the trigger falls back to
-- morning_day rates for the full duration.  For correct weekend/holiday
-- pricing, use the Python calculate_booking_price() function.

CREATE OR REPLACE FUNCTION bookings_compute_fields() RETURNS trigger AS $$
DECLARE
    _fmt TEXT;
    _start_min INT;
    _end_min INT;
    _overlap INT;
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
        -- Treat 23:59:59 as midnight (1440)
        IF EXTRACT(HOUR FROM NEW.time_end) = 23
           AND EXTRACT(MINUTE FROM NEW.time_end) = 59
           AND EXTRACT(SECOND FROM NEW.time_end) >= 59 THEN
            _end_min := 1440;
        END IF;

        IF _end_min <= _start_min THEN
            _end_min := 1440;
        END IF;

        -- Walk through each pricing period and accumulate cost
        FOR _period IN
            SELECT pricing_type, price_per_hour,
                   CASE pricing_type
                       WHEN 'after_midnight' THEN 0
                       WHEN 'morning_day'    THEN 420
                       WHEN 'evening'        THEN 1110
                       WHEN 'late_night'     THEN 1320
                   END AS p_start,
                   CASE pricing_type
                       WHEN 'after_midnight' THEN 420
                       WHEN 'morning_day'    THEN 1110
                       WHEN 'evening'        THEN 1320
                       WHEN 'late_night'     THEN 1440
                   END AS p_end
            FROM field_prices
            WHERE format_name = _fmt
              AND pricing_type IN ('after_midnight','morning_day','evening','late_night')
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
