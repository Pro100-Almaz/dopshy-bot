-- Replace exact-slot uniqueness with a true overlap guard. Scoped to the states
-- that actually hold a slot (awaiting_payment, confirmed) so DRAFT rows — which
-- may have NULL start_at/end_at before the user picks a time — never conflict.

CREATE EXTENSION IF NOT EXISTS btree_gist;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'bookings_no_overlap'
    ) THEN
        ALTER TABLE bookings
            ADD CONSTRAINT bookings_no_overlap
            EXCLUDE USING gist (
                field WITH =,
                tstzrange(start_at, end_at) WITH &&
            )
            WHERE (state IN ('awaiting_payment', 'confirmed'));
    END IF;
END $$;
