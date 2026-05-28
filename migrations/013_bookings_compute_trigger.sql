-- Make every booking write safe regardless of the writer (service layer,
-- manager_api, Directus, manual SQL). Computes:
--   start_at / end_at   from (date + time_*) at Asia/Almaty
--   price_total         from fields.price_per_hour × hours (when missing)
--   updated_at          on every change
-- so the EXCLUDE constraint always has correct ranges to check against.

CREATE OR REPLACE FUNCTION bookings_compute_fields() RETURNS trigger AS $$
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
        SELECT price_per_hour * EXTRACT(EPOCH FROM (NEW.time_end - NEW.time_start)) / 3600.0
        INTO NEW.price_total
        FROM fields WHERE id = NEW.field;
    END IF;

    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS bookings_compute_fields_trg ON bookings;
CREATE TRIGGER bookings_compute_fields_trg
    BEFORE INSERT OR UPDATE ON bookings
    FOR EACH ROW EXECUTE FUNCTION bookings_compute_fields();
