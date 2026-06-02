-- Client self-service edit support.
--
-- Clients may modify a confirmed/awaiting_payment booking once, provided the
-- game starts more than 48 hours in the future. The edit is implemented as
-- "cancel old + insert new" inside a single transaction so the existing
-- `bookings_no_overlap` EXCLUDE constraint protects the new slot for free.
--
--   client_edited_at         : set on the OLD row when an edit succeeds. The
--                              same row also flips to state='cancelled'. The
--                              once-only rule walks back through predecessors,
--                              so a single TIMESTAMPTZ on the cancelled row is
--                              enough — no separate flag on the new row.
--   predecessor_booking_id   : on the NEW row, points at the OLD (cancelled)
--                              row. Indexed for the chain walk in
--                              booking_service.client_edit_booking().

ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS client_edited_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS predecessor_booking_id INTEGER
        REFERENCES bookings(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS bookings_predecessor_idx
    ON bookings (predecessor_booking_id)
    WHERE predecessor_booking_id IS NOT NULL;
