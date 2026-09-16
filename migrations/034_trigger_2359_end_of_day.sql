-- Align the bookings_compute_fields trigger with the Python pricing logic for
-- the end-of-day sentinel.
--
-- A booking that runs "until the end of the day" is normalized to the 23:59
-- sentinel (normalize_end_time), but stored as 23:59:00 for single bookings and
-- as 23:59:59 for the first half of a transitive (midnight-crossing) pair. The
-- previous trigger only rounded 23:59 up to midnight (1440) when the seconds
-- were >= 59, so a single booking stored as 23:59:00 was priced as 1439 minutes
-- and lost its final minute. Python's _to_minutes() treats ANY 23:59[:ss] as
-- 1440, so the stored price_total disagreed with the price shown to the client
-- (e.g. 53550 in the DB vs 54000 on WhatsApp for a 22:00-00:00 late-night slot).
--
-- Drop the seconds check so the trigger matches Python: any 23:59 is end-of-day.

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
        -- Treat any 23:59 (regardless of seconds) as end-of-day midnight (1440),
        -- matching Python's _to_minutes(). Single end-of-day bookings are stored
        -- as 23:59:00; the transitive first half is 23:59:59 — both are 1440.
        IF EXTRACT(HOUR FROM NEW.time_end) = 23
           AND EXTRACT(MINUTE FROM NEW.time_end) = 59 THEN
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
