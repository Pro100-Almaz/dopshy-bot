"""Tests for booking_service.client_edit_booking — the self-service edit flow.

Edits use wall-clock-relative dates (today+N) because the 48h window is
enforced by `NOW()` in SQL, not by a configurable clock. Tests pin the
booking >48h out for happy paths and <48h out for the window-closed case.
"""

from datetime import date, timedelta
import uuid

import psycopg2.extras

from integrations import booking_service as svc
from integrations.repo.postgres import _conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _token() -> str:
    return str(uuid.uuid4())


def _state(bid: int) -> str:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM bookings WHERE id = %s", (bid,))
            return cur.fetchone()[0]


def _events(bid: int) -> list[str]:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT event FROM booking_events WHERE booking_id = %s ORDER BY id",
                (bid,),
            )
            return [r[0] for r in cur.fetchall()]


def _row(bid: int) -> dict:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM bookings WHERE id = %s", (bid,))
            row = cur.fetchone()
            return dict(row) if row else {}


def _confirmed_booking(phone: str = "7700",
                       days_ahead: int = 7,
                       time_start: str = "18:00",
                       time_end: str = "19:00",
                       field: int = 1) -> int:
    """Create a fully-confirmed booking days_ahead days into the future."""
    d = (date.today() + timedelta(days=days_ahead)).isoformat()
    token = _token()
    res = svc.create_draft("chatX", phone, token,
                           date=d, time_start=time_start, time_end=time_end,
                           field=field, format="5x5", players=8,
                           customer_name="Test")
    bid = res["data"]["booking_id"]
    pay = svc.request_payment(bid, token)
    assert pay["ok"], pay
    # Force-confirm without going through payment validation.
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE bookings SET state = 'confirmed' WHERE id = %s", (bid,))
    return bid


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_client_edit_success_moves_to_new_slot():
    bid = _confirmed_booking(days_ahead=7, time_start="18:00", time_end="19:00", field=1)
    res = svc.client_edit_booking(
        bid, time_start="20:00", time_end="21:00", actor_id="chatX",
    )
    assert res["ok"], res

    new_id = res["data"]["booking_id"]
    assert new_id != bid
    assert _state(bid) == "cancelled"
    assert _state(new_id) == "confirmed"

    new = _row(new_id)
    assert str(new["time_start"])[:5] == "20:00"
    assert str(new["time_end"])[:5] == "21:00"
    assert new["predecessor_booking_id"] == bid
    assert new["field"] == 1
    assert new["phone"] == "7700"

    old = _row(bid)
    assert old["client_edited_at"] is not None

    assert "client_edit_cancelled" in _events(bid)
    assert "client_edited" in _events(new_id)


def test_client_edit_preserves_awaiting_payment_state():
    """Editing an unpaid booking should keep it awaiting_payment, not jump to confirmed."""
    d = (date.today() + timedelta(days=7)).isoformat()
    token = _token()
    bid = svc.create_draft("chatX", "7700", token,
                            date=d, time_start="18:00", time_end="19:00",
                            field=1, format="5x5", players=8,
                            customer_name="Test")["data"]["booking_id"]
    svc.request_payment(bid, token)
    assert _state(bid) == "awaiting_payment"

    res = svc.client_edit_booking(bid, time_start="20:00", time_end="21:00")
    assert res["ok"]
    assert _state(res["data"]["booking_id"]) == "awaiting_payment"


def test_client_edit_changes_field_recomputes_price():
    """Switching from field 1 (5x5, 35000) to field 2 (6x6, 45000) should reprice."""
    bid = _confirmed_booking(days_ahead=7, time_start="18:00", time_end="19:00", field=1)
    res = svc.client_edit_booking(bid, field=2)
    assert res["ok"], res
    new = _row(res["data"]["booking_id"])
    assert new["field"] == 2
    # 1 hour × 45000 = 45000
    assert float(new["price_total"]) == 45000.0


# ---------------------------------------------------------------------------
# Window closed
# ---------------------------------------------------------------------------

def test_client_edit_within_48h_rejected():
    bid = _confirmed_booking(days_ahead=1, time_start="18:00", time_end="19:00")
    res = svc.client_edit_booking(bid, time_start="20:00", time_end="21:00")
    assert not res["ok"]
    assert res["code"] == "EDIT_WINDOW_CLOSED"
    # Original untouched
    assert _state(bid) == "confirmed"
    assert _row(bid)["client_edited_at"] is None


# ---------------------------------------------------------------------------
# Already edited (predecessor chain)
# ---------------------------------------------------------------------------

def test_client_edit_chain_only_one_allowed():
    bid = _confirmed_booking(days_ahead=7)
    r1 = svc.client_edit_booking(bid, time_start="20:00", time_end="21:00")
    assert r1["ok"]
    new_id = r1["data"]["booking_id"]

    r2 = svc.client_edit_booking(new_id, time_start="14:00", time_end="15:00")
    assert not r2["ok"]
    assert r2["code"] == "ALREADY_EDITED"
    assert _state(new_id) == "confirmed"  # new row still active


# ---------------------------------------------------------------------------
# Slot taken
# ---------------------------------------------------------------------------

def test_client_edit_slot_taken_by_third_booking():
    bid1 = _confirmed_booking(phone="7700", days_ahead=7,
                              time_start="18:00", time_end="19:00", field=1)
    bid2 = _confirmed_booking(phone="7711", days_ahead=7,
                              time_start="20:00", time_end="21:00", field=1)
    # bid1 tries to move onto bid2's slot
    res = svc.client_edit_booking(bid1, time_start="20:00", time_end="21:00")
    assert not res["ok"]
    assert res["code"] == "SLOT_TAKEN"
    # Both originals unchanged
    assert _state(bid1) == "confirmed"
    assert _state(bid2) == "confirmed"
    assert _row(bid1)["client_edited_at"] is None


# ---------------------------------------------------------------------------
# No change
# ---------------------------------------------------------------------------

def test_client_edit_no_diff_returns_no_change():
    bid = _confirmed_booking(days_ahead=7)
    res = svc.client_edit_booking(bid)
    assert not res["ok"]
    assert res["code"] == "NO_CHANGE"


def test_client_edit_same_values_returns_no_change():
    bid = _confirmed_booking(days_ahead=7, time_start="18:00", time_end="19:00")
    res = svc.client_edit_booking(bid, time_start="18:00", time_end="19:00")
    assert not res["ok"]
    assert res["code"] == "NO_CHANGE"


# ---------------------------------------------------------------------------
# Invalid state
# ---------------------------------------------------------------------------

def test_client_edit_rejects_cancelled_booking():
    bid = _confirmed_booking(days_ahead=7)
    svc.cancel_booking(bid)
    res = svc.client_edit_booking(bid, time_start="20:00", time_end="21:00")
    assert not res["ok"]
    assert res["code"] == "INVALID_STATE"


def test_client_edit_rejects_draft_booking():
    d = (date.today() + timedelta(days=7)).isoformat()
    bid = svc.create_draft("chatX", "7700", _token(),
                            date=d, time_start="18:00", time_end="19:00",
                            field=1, format="5x5", players=8,
                            customer_name="Test")["data"]["booking_id"]
    res = svc.client_edit_booking(bid, time_start="20:00", time_end="21:00")
    assert not res["ok"]
    assert res["code"] == "INVALID_STATE"


# ---------------------------------------------------------------------------
# Payments are re-pointed
# ---------------------------------------------------------------------------

def test_client_edit_forwards_payments():
    bid = _confirmed_booking(days_ahead=7)
    ref = f"ref-{uuid.uuid4().hex[:10]}"
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO payments (booking_id, method, bank, amount, "
                "  transaction_ref, status, verified_at) "
                "VALUES (%s, 'bank_transfer', 'kaspi', 17500, %s, 'accepted', NOW())",
                (bid, ref),
            )
    res = svc.client_edit_booking(bid, time_start="20:00", time_end="21:00")
    assert res["ok"]
    new_id = res["data"]["booking_id"]

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM payments WHERE booking_id = %s", (new_id,))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT COUNT(*) FROM payments WHERE booking_id = %s", (bid,))
            assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Diff filtering
# ---------------------------------------------------------------------------

def test_client_edit_ignores_none_fields():
    """The LLM may pass null for fields the user didn't specify."""
    bid = _confirmed_booking(days_ahead=7, time_start="18:00", time_end="19:00")
    res = svc.client_edit_booking(
        bid,
        time_start="20:00", time_end="21:00",
        date=None, field=None, players=None, customer_name=None,
    )
    assert res["ok"]
    new = _row(res["data"]["booking_id"])
    assert str(new["time_start"])[:5] == "20:00"


def test_client_edit_only_name_change():
    bid = _confirmed_booking(days_ahead=7)
    res = svc.client_edit_booking(bid, customer_name="Алмат")
    assert res["ok"]
    new = _row(res["data"]["booking_id"])
    assert new["customer_name"] == "Алмат"
    # Slot unchanged
    old = _row(bid)
    assert new["field"] == old["field"]
    assert new["time_start"] == old["time_start"]
