-- Backfill the new columns from the legacy data.

-- start_at / end_at: interpret the naive (date + time) as Almaty local time.
UPDATE bookings
SET start_at = (date + time_start) AT TIME ZONE 'Asia/Almaty',
    end_at   = (date + time_end)   AT TIME ZONE 'Asia/Almaty'
WHERE start_at IS NULL OR end_at IS NULL;

-- state: translate legacy status values.
UPDATE bookings
SET state = CASE status
                WHEN 'awaiting_payment' THEN 'awaiting_payment'
                WHEN 'paid'             THEN 'confirmed'
                WHEN 'completed'        THEN 'confirmed'
                WHEN 'cancelled'        THEN 'cancelled'
                ELSE 'confirmed'
            END
WHERE state IS NULL;
