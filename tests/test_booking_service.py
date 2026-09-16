"""Unit tests for the booking service layer."""

import uuid

import pytest

from integrations import booking_service
from integrations.repo import postgres as svc
from integrations.repo.postgres import _conn


def _token() -> str:
    return str(uuid.uuid4())


def _state(booking_id: int) -> str:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM bookings WHERE id = %s", (booking_id,))
            return cur.fetchone()[0]


def _events(booking_id: int) -> list[str]:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT event FROM booking_events WHERE booking_id = %s ORDER BY id",
                (booking_id,),
            )
            return [r[0] for r in cur.fetchall()]


def _ready_draft(token, date="2026-07-01", ts="18:00", te="19:00", field=1):
    """Create a fully-populated DRAFT ready for request_payment."""
    res = svc.create_draft("dopsy_bot", chat_id="chat_test",
                           date=date, time_start=ts, time_end=te,
                           field=field, format="5x5", players=8,
                           customer_name="Test")
    return res["data"]["booking_id"]


# ---------------------------------------------------------------------------

def test_create_draft_is_idempotent():
    token = _token()
    a = svc.create_draft("dopsy_bot", phone="7700", chat_id="chat_test", client_token=token)
    b = svc.create_draft("dopsy_bot", phone="7700", chat_id="chat_test", client_token=token)
    assert a["ok"] and b["ok"]
    assert a["data"]["booking_id"] == b["data"]["booking_id"]
    # Only one draft_created event despite two calls.
    assert _events(a["data"]["booking_id"]).count("draft_created") == 1


def test_update_draft_sets_fields():
    token = _token()
    bid = svc.create_draft("dopsy_bot", phone="7700", chat_id="chat_test", client_token=token)["data"]["booking_id"]
    res = svc.update_draft(bot_name='dopsy_bot', object_id=bid, date="2026-07-01", time_start="10:00", time_end="11:00", field=2)
    assert res["ok"]
    assert _state(bid) == "draft"


def test_update_draft_rejected_after_payment():
    token = _token()
    bid = _ready_draft(token)
    booking_service.request_payment(bid, token)
    res = svc.update_draft('dopsy_bot', object_id=bid, players=99)
    assert not res["ok"]
    assert res["code"] == "BOOKING_WRONG_STATE"


def test_request_payment_success():
    token = _token()
    bid = _ready_draft(token)
    res = booking_service.request_payment(bid, token)
    assert res["ok"]
    assert _state(bid) == "awaiting_payment"
    assert res["data"]["reserved_until"] is not None
    assert "payment_requested" in _events(bid)


def test_request_payment_missing_fields():
    token = _token()
    bid = svc.create_draft("dopsy_bot", phone="7700", chat_id="chat_test", client_token=token)["data"]["booking_id"]
    res = booking_service.request_payment(bid, token)
    assert not res["ok"]
    assert res["code"] == "INVALID_TIME"


def test_request_payment_slot_taken():
    t1, t2 = _token(), _token()
    b1 = _ready_draft(t1, field=1, ts="18:00", te="19:00")
    b2 = _ready_draft(t2, field=1, ts="18:30", te="19:30")  # overlaps b1
    r1 = booking_service.request_payment(b1, t1)
    r2 = booking_service.request_payment(b2, t2)
    assert r1["ok"]
    assert not r2["ok"]
    assert r2["code"] == "SLOT_TAKEN"
    assert _state(b2) == "draft"  # unchanged


def test_request_payment_idempotent():
    token = _token()
    bid = _ready_draft(token)
    booking_service.request_payment(bid, token)
    res = booking_service.request_payment(bid, token)
    assert res["ok"]
    assert _state(bid) == "awaiting_payment"


def test_submit_payment_proof_confirms():
    token = _token()
    bid = _ready_draft(token)
    booking_service.request_payment(bid, token)
    res = booking_service.submit_payment_proof(bid, proof_media_id="media123")
    assert res["ok"]
    assert _state(bid) == "confirmed"
    assert "payment_received" in _events(bid)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT proof_media_id FROM payments WHERE booking_id = %s", (bid,))
            assert cur.fetchone()[0] == "media123"


def test_submit_payment_proof_wrong_state():
    token = _token()
    bid = svc.create_draft("dopsy_bot", phone="7700", chat_id="chat_test", client_token=token)["data"]["booking_id"]
    res = booking_service.submit_payment_proof(bid)
    assert not res["ok"]
    assert res["code"] == "BOOKING_WRONG_STATE"


def test_cancel_releases_slot():
    t1 = _token()
    b1 = _ready_draft(t1, field=1, ts="18:00", te="19:00")
    booking_service.request_payment(b1, t1)
    booking_service.submit_payment_proof(b1)
    assert _state(b1) == "confirmed"

    # Same slot is blocked while b1 confirmed.
    t2 = _token()
    b2 = _ready_draft(t2, field=1, ts="18:00", te="19:00")
    assert booking_service.request_payment(b2, t2)["code"] == "SLOT_TAKEN"

    # Cancel b1 → slot frees up.
    assert svc.cancel_booking_trial('dopsy_bot', b1)["ok"]
    t3 = _token()
    b3 = _ready_draft(t3, field=1, ts="18:00", te="19:00")
    assert booking_service.request_payment(b3, t3)["ok"]


def test_manager_create_booking_confirmed():
    res = booking_service.manager_create_booking(
        field=2, date="2026-07-05", end_date="2026-07-05",
        time_start="12:00", time_end="13:00",
        customer="Манагер", phone="7701", actor_id="mgr-key",
    )
    assert res["ok"]
    bid = res["data"]["booking_id"]
    assert _state(bid) == "confirmed"
    assert "manager_created" in _events(bid)


def test_manager_create_booking_slot_taken():
    booking_service.manager_create_booking(field=2, date="2026-07-05", end_date="2026-07-05",
                               time_start="12:00", time_end="13:00")
    res = booking_service.manager_create_booking(field=2, date="2026-07-05", end_date="2026-07-05",
                                     time_start="12:30", time_end="13:30")
    assert not res["ok"]
    assert res["code"] == "SLOT_TAKEN"


def _count_bookings() -> int:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM bookings")
            return cur.fetchone()[0]


def test_batch_creates_all_slots():
    res = booking_service.manager_create_bookings_batch(
        [{"field": 1, "date": "2026-08-01", "time_start": "10:00", "time_end": "11:00"},
         {"field": 2, "date": "2026-08-01", "time_start": "10:00", "time_end": "11:00"},
         {"field": 3, "date": "2026-08-01", "time_start": "10:00", "time_end": "11:00"}],
        customer="Батч", phone="7702",
    )
    assert res["ok"]
    created = res["data"]["created"]
    assert len(created) == 3
    assert _count_bookings() == 3
    for r in created:
        assert "manager_created" in _events(r["booking_id"])


def test_batch_is_atomic_on_conflict():
    """If any slot in the batch clashes, NONE of the batch is persisted."""
    # Pre-existing booking that the second batch slot will collide with.
    pre = booking_service.manager_create_booking(
        field=2, date="2026-08-02", end_date="2026-08-02",
        time_start="12:00", time_end="13:00",
    )
    assert pre["ok"]
    assert _count_bookings() == 1

    res = booking_service.manager_create_bookings_batch(
        [{"field": 1, "date": "2026-08-02", "time_start": "10:00", "time_end": "11:00"},
         {"field": 2, "date": "2026-08-02", "time_start": "12:30", "time_end": "13:30"},  # clashes
         {"field": 3, "date": "2026-08-02", "time_start": "10:00", "time_end": "11:00"}],
    )
    assert not res["ok"]
    assert res["code"] == "SLOT_TAKEN"
    # The whole batch rolled back — only the pre-existing booking remains.
    assert _count_bookings() == 1


def test_batch_empty_slots_returns_invalid():
    res = booking_service.manager_create_bookings_batch([])
    assert not res["ok"]
    assert res["code"] == "INVALID"


@pytest.mark.parametrize("midnight_end", ["23:59", "24:00"])
def test_batch_merges_midnight_sentinel(midnight_end):
    res = booking_service.manager_create_bookings_batch(
        [{"field": 1, "date": "2026-08-10", "time_start": "23:00", "time_end": midnight_end},
         {"field": 1, "date": "2026-08-11", "time_start": "00:00", "time_end": "01:00"}],
        customer="Полночь", phone="7703",
    )
    assert res["ok"]
    created = res["data"]["created"]
    # One logical cross-midnight booking = two rows (pre- and post-midnight halves).
    assert len(created) == 2
    ids = [r["booking_id"] for r in created]
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT time_start, time_end, group_transition FROM bookings "
                "WHERE id = ANY(%s) ORDER BY date, time_start",
                (ids,),
            )
            rows = cur.fetchall()
    # First half runs 23:00 -> midnight, second half 00:00 -> 01:00.
    assert str(rows[0][0]).startswith("23:00")
    assert str(rows[0][1]).startswith("23:59")
    assert str(rows[1][0]).startswith("00:00")
    assert str(rows[1][1]).startswith("01:00")
    # Both halves are linked as the same booking via group_transition.
    assert rows[0][2] == rows[1][2]


def _age_row(booking_id, *, reserved_until=None, created_at=None):
    with _conn() as conn:
        with conn.cursor() as cur:
            if reserved_until is not None:
                cur.execute("UPDATE bookings SET reserved_until = %s WHERE id = %s",
                            (reserved_until, booking_id))
            if created_at is not None:
                cur.execute("UPDATE bookings SET created_at = %s WHERE id = %s",
                            (created_at, booking_id))


def test_reject_payment_allows_retry():
    """Rejected receipt must not flip state to failed — the user should be able to
    resubmit a valid receipt within the TTL window."""
    token = _token()
    bid = _ready_draft(token)
    booking_service.request_payment(bid, token)
    assert _state(bid) == "awaiting_payment"

    booking_service.reject_payment(bid, "amount too low",
                       parsed={"bank": "kaspi", "amount": 1000, "ref": None})
    assert _state(bid) == "awaiting_payment", "rejected receipt must not change state"
    assert "payment_rejected" in _events(bid)

    # User resubmits a valid receipt → confirms normally.
    ok = booking_service.submit_payment_proof(bid, parsed={"bank": "kaspi", "amount": 35000,
                                                "ref": "GOOD-REF-1", "date": None})
    assert ok["ok"]
    assert _state(bid) == "confirmed"


def test_cancel_clears_linked_session():
    token = _token()
    bid = svc.create_draft('dopsy_bot', chat_id="chatX", phone="7700", client_token=token)["data"]["booking_id"]
    svc.upsert_session('dopsy_bot', "chatX", "step_date",
                        {"booking_id": bid, "available_days": []}, bid)
    assert svc.get_active_session('dopsy_bot', "chatX") is not None

    svc.cancel_booking_trial('dopsy_bot', object_id=bid, actor_type="chatbot:Бот", reason="test")

    assert svc.get_active_session('dopsy_bot', "chatX") is None


def test_get_expired_bookings_selects_expired_only():
    from integrations.repo import booking_repo as repo

    # Expired awaiting_payment (reserved_until in the past).
    t1 = _token()
    b_expired = _ready_draft(t1, field=1, ts="08:00", te="09:00")
    booking_service.request_payment(b_expired, t1)
    _age_row(b_expired, reserved_until="2000-01-01 00:00:00+00")

    # Fresh awaiting_payment (reserved_until in the future) — must NOT be swept.
    t2 = _token()
    b_fresh = _ready_draft(t2, field=2, ts="08:00", te="09:00")
    booking_service.request_payment(b_fresh, t2)

    # Abandoned draft (created long ago).
    t3 = _token()
    b_draft = svc.create_draft("c", phone="7700", chat_id=t3)["data"]["booking_id"]
    _age_row(b_draft, created_at="2000-01-01 00:00:00+00")

    expired_ids = {b["id"] for b in repo.get_expired_bookings(1800)}
    assert b_expired in expired_ids
    assert b_draft in expired_ids
    assert b_fresh not in expired_ids
