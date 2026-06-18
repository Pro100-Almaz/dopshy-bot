-- TRANSITIVE BOOKING: trigger to sync fields across bookings with same group_transition.
-- When one instance's state, source, customer_name, or field changes,
-- other instances with the same group_transition UUID are updated automatically.
-- This ensures the two halves of a day-crossing booking always stay in sync.

CREATE OR REPLACE FUNCTION bookings_sync_group_transition() RETURNS trigger AS $$
BEGIN
    -- Prevent infinite recursion: skip if we are already inside this trigger
    IF current_setting('app.syncing_group_transition', true) = 'true' THEN
        RETURN NEW;
    END IF;

    IF NEW.group_transition IS NOT NULL AND (
        OLD.state IS DISTINCT FROM NEW.state OR
        OLD.source IS DISTINCT FROM NEW.source OR
        OLD.customer_name IS DISTINCT FROM NEW.customer_name OR
        OLD.field IS DISTINCT FROM NEW.field
    ) THEN
        PERFORM set_config('app.syncing_group_transition', 'true', true);
        UPDATE bookings SET
            state         = NEW.state,
            source        = NEW.source,
            customer_name = NEW.customer_name,
            field         = NEW.field,
            updated_at    = NOW()
        WHERE group_transition = NEW.group_transition
          AND id != NEW.id;
        PERFORM set_config('app.syncing_group_transition', 'false', true);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS bookings_sync_group_transition_trg ON bookings;
CREATE TRIGGER bookings_sync_group_transition_trg
    AFTER UPDATE ON bookings
    FOR EACH ROW EXECUTE FUNCTION bookings_sync_group_transition();
