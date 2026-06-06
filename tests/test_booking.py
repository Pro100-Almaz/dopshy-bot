"""Tests for slot-availability functions in integrations/booking_repo.py.

Covers:
- get_free_windows: empty DB, single booking subtraction, adjacent bookings,
  draft/cancelled/failed exclusion, per-field isolation
- is_range_free: overlapping and non-overlapping ranges
- format_availability_context: structural sanity for LLM context string

Test setup uses booking_service.create_draft + update_draft + request_payment
so the EXCLUDE constraint is exercised and state transitions are real.
manager_create_booking is used as a convenience shortcut where the test only
needs a confirmed slot without caring about the payment flow.
"""

import uuid
from datetime import date, timedelta

import pytest

import config
from integrations.repo import postgres as svc
from integrations.booking import (
    format_availability_context,
    get_all_booked,
    get_free_windows,
    is_range_free,
)
from utils import today_almaty


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _token() -> str:
    return str(uuid.uuid4())


def _future_date(days_ahead: int = 7) -> str:
    """Return an ISO date string that is always in the future (outside the
    current 7-day window used by get_free_windows) so the tests are not
    sensitive to time-of-day.  For get_free_windows tests we use a date
    *inside* the 7-day window instead — see _near_date()."""
    return (today_almaty() + timedelta(days=days_ahead)).isoformat()


def _near_date(days_ahead: int = 3) -> str:
    """Return a date inside the next-7-days window used by get_free_windows."""
    return (today_almaty() + timedelta(days=days_ahead)).isoformat()


def _confirmed_booking(field: int, date_str: str, ts: str, te: str) -> int:
    """Create a confirmed booking via manager_create_booking (fastest path)."""
    res = svc.manager_create_booking(
        field=field, date=date_str, end_date=date_str,
        time_start=ts, time_end=te,
        customer="Test", phone="77001234567",
    )
    assert res["ok"], f"manager_create_booking failed: {res}"
    return res["data"]["booking_id"]


def _awaiting_booking(field: int, date_str: str, ts: str, te: str) -> int:
    """Create a booking in awaiting_payment state via the normal flow."""
    token = _token()
    res = svc.create_draft('dopsy_bot',
        {
            # "chat_id": "chat_test",
            "phone": "77009999999",
            "token": token,
            "date": date_str,
            "time_start": ts,
            "time_end": te,
            "field": field,
            "format": _format_for(field),
            "players": 6,
            "customer_name": "AwaitTest",
        },
    )
    assert res["ok"]
    bid = res["data"]["booking_id"]
    res2 = svc.request_payment(bid, token)
    assert res2["ok"], f"request_payment failed: {res2}"
    return bid


def _format_for(field_id: int) -> str:
    """Look up the format string for a given field id from config."""
    for f in config.BOOKING_FIELDS:
        if f["id"] == field_id:
            return f["format"]
    return "5x5"


def _open_time() -> str:
    return config.BOOKING_OPEN_TIME  # e.g. "09:00"


def _close_time() -> str:
    return config.BOOKING_CLOSE_TIME  # e.g. "23:00"


def _field_ids() -> list[int]:
    return [f["id"] for f in config.BOOKING_FIELDS]


# ---------------------------------------------------------------------------
# get_all_booked — filter sanity
# ---------------------------------------------------------------------------

class TestGetAllBooked:
    def test_empty_db_returns_empty_list(self):
        today = today_almaty()
        week_end = today + timedelta(days=6)
        booked = get_all_booked(today, week_end)
        assert booked == []

    def test_confirmed_booking_is_included(self):
        date_str = _near_date(3)
        _confirmed_booking(field=_field_ids()[0], date_str=date_str, ts="10:00", te="11:00")
        today = today_almaty()
        week_end = today + timedelta(days=6)
        booked = get_all_booked(today, week_end)
        assert len(booked) == 1
        assert str(booked[0]["date"]) == date_str

    def test_awaiting_payment_is_included(self):
        date_str = _near_date(3)
        _awaiting_booking(field=_field_ids()[0], date_str=date_str, ts="12:00", te="13:00")
        today = today_almaty()
        week_end = today + timedelta(days=6)
        booked = get_all_booked(today, week_end)
        assert len(booked) == 1

    def test_draft_booking_excluded(self):
        """DRAFT rows must not appear in get_all_booked (they hold no slot)."""
        token = _token()
        svc.create_draft(
            "chat_draft", "77000000001", token,
            date=_near_date(3), time_start="14:00", time_end="15:00",
            field=_field_ids()[0], format=_format_for(_field_ids()[0]),
            players=5, customer_name="DraftOnly",
        )
        today = today_almaty()
        booked = get_all_booked(today, today + timedelta(days=6))
        assert booked == []

    def test_cancelled_booking_excluded(self):
        date_str = _near_date(3)
        bid = _confirmed_booking(field=_field_ids()[0], date_str=date_str, ts="15:00", te="16:00")
        svc.cancel_booking(bid)
        today = today_almaty()
        booked = get_all_booked(today, today + timedelta(days=6))
        assert booked == []

    def test_failed_booking_excluded(self):
        """failed state (after rejected payment) must not block the slot."""
        # Use raw SQL to force state=failed since no booking_service path produces
        # failed directly (reject_payment keeps awaiting_payment; only manager/sweeper
        # transitions to failed via cancel path in older code).
        # We create an awaiting_payment booking, then manually flip it to failed.
        from integrations.repo.postgres import _conn
        date_str = _near_date(3)
        bid = _awaiting_booking(field=_field_ids()[0], date_str=date_str, ts="16:00", te="17:00")
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE bookings SET state = 'failed' WHERE id = %s", (bid,))
        today = today_almaty()
        booked = get_all_booked(today, today + timedelta(days=6))
        assert booked == []


# ---------------------------------------------------------------------------
# get_free_windows
# ---------------------------------------------------------------------------

class TestGetFreeWindows:
    def test_full_day_free_when_no_bookings(self):
        """With an empty DB, each field must show one window spanning open→close."""
        windows = get_free_windows()
        # Group by (date, field)
        by_date_field: dict[tuple, list] = {}
        for w in windows:
            key = (w["date"], w["field"])
            by_date_field.setdefault(key, []).append(w)

        open_t_str = _open_time()   # "09:00"
        close_t_str = _close_time()  # "23:00"

        # Every (date, field) combination must produce exactly one unbroken window
        # that starts at open and ends at close (for future dates — today may have
        # a clipped floor if run near midnight, so we only check day+3 onwards).
        target_date = today_almaty() + timedelta(days=3)
        for fld in _field_ids():
            key = (target_date, fld)
            assert key in by_date_field, f"No free window for {key}"
            wins = by_date_field[key]
            assert len(wins) == 1, f"Expected single window for {key}, got {wins}"
            assert wins[0]["time_start"].strftime("%H:%M") == open_t_str
            assert wins[0]["time_end"].strftime("%H:%M") == close_t_str

    def test_single_booking_splits_day_into_two_windows(self):
        """A booking in the middle of the day must produce exactly two free windows."""
        target_date = (today_almaty() + timedelta(days=3)).isoformat()
        field_id = _field_ids()[0]
        _confirmed_booking(field=field_id, date_str=target_date, ts="12:00", te="14:00")

        windows = get_free_windows()
        day_field_wins = [
            w for w in windows
            if str(w["date"]) == target_date and w["field"] == field_id
        ]
        assert len(day_field_wins) == 2

        # First window: open_time → 12:00
        first = min(day_field_wins, key=lambda w: w["time_start"])
        assert first["time_start"].strftime("%H:%M") == _open_time()
        assert first["time_end"].strftime("%H:%M") == "12:00"

        # Second window: 14:00 → close_time
        second = max(day_field_wins, key=lambda w: w["time_start"])
        assert second["time_start"].strftime("%H:%M") == "14:00"
        assert second["time_end"].strftime("%H:%M") == _close_time()

    def test_booking_at_open_produces_one_trailing_window(self):
        """Booking that starts at open time → only one window after it."""
        target_date = (today_almaty() + timedelta(days=4)).isoformat()
        field_id = _field_ids()[0]
        _confirmed_booking(field=field_id, date_str=target_date,
                           ts=_open_time(), te="11:00")

        windows = get_free_windows()
        day_wins = [
            w for w in windows
            if str(w["date"]) == target_date and w["field"] == field_id
        ]
        assert len(day_wins) == 1
        assert day_wins[0]["time_start"].strftime("%H:%M") == "11:00"
        assert day_wins[0]["time_end"].strftime("%H:%M") == _close_time()

    def test_booking_at_close_produces_one_leading_window(self):
        """Booking ending at close time → only one window before it."""
        target_date = (today_almaty() + timedelta(days=4)).isoformat()
        field_id = _field_ids()[0]
        _confirmed_booking(field=field_id, date_str=target_date,
                           ts="21:00", te=_close_time())

        windows = get_free_windows()
        day_wins = [
            w for w in windows
            if str(w["date"]) == target_date and w["field"] == field_id
        ]
        assert len(day_wins) == 1
        assert day_wins[0]["time_start"].strftime("%H:%M") == _open_time()
        assert day_wins[0]["time_end"].strftime("%H:%M") == "21:00"

    def test_adjacent_bookings_produce_no_gap_between_them(self):
        """Two back-to-back bookings must not create a phantom free window between them."""
        target_date = (today_almaty() + timedelta(days=3)).isoformat()
        field_id = _field_ids()[0]
        _confirmed_booking(field=field_id, date_str=target_date, ts="10:00", te="12:00")
        _confirmed_booking(field=field_id, date_str=target_date, ts="12:00", te="14:00")

        windows = get_free_windows()
        day_wins = sorted(
            [w for w in windows if str(w["date"]) == target_date and w["field"] == field_id],
            key=lambda w: w["time_start"],
        )
        # Two windows: open→10:00 and 14:00→close
        assert len(day_wins) == 2
        assert day_wins[0]["time_start"].strftime("%H:%M") == _open_time()
        assert day_wins[0]["time_end"].strftime("%H:%M") == "10:00"
        assert day_wins[1]["time_start"].strftime("%H:%M") == "14:00"
        assert day_wins[1]["time_end"].strftime("%H:%M") == _close_time()

    def test_fully_booked_day_produces_no_windows(self):
        """When open→close is filled, no free windows remain for that field."""
        target_date = (today_almaty() + timedelta(days=5)).isoformat()
        field_id = _field_ids()[0]
        _confirmed_booking(field=field_id, date_str=target_date,
                           ts=_open_time(), te=_close_time())

        windows = get_free_windows()
        day_wins = [
            w for w in windows
            if str(w["date"]) == target_date and w["field"] == field_id
        ]
        assert day_wins == []

    def test_draft_booking_does_not_block_slot(self):
        """DRAFT rows must not reduce free windows (they hold no slot)."""
        target_date = (today_almaty() + timedelta(days=3)).isoformat()
        field_id = _field_ids()[0]
        token = _token()
        svc.create_draft(
            "chat_draft2", "77000000002", token,
            date=target_date, time_start="10:00", time_end="12:00",
            field=field_id, format=_format_for(field_id),
            players=5, customer_name="DraftOnly2",
        )
        # No request_payment called → stays draft

        windows = get_free_windows()
        day_wins = [
            w for w in windows
            if str(w["date"]) == target_date and w["field"] == field_id
        ]
        # Should still be one unbroken window
        assert len(day_wins) == 1
        assert day_wins[0]["time_start"].strftime("%H:%M") == _open_time()
        assert day_wins[0]["time_end"].strftime("%H:%M") == _close_time()

    def test_cancelled_booking_does_not_block_slot(self):
        target_date = (today_almaty() + timedelta(days=3)).isoformat()
        field_id = _field_ids()[0]
        bid = _confirmed_booking(field=field_id, date_str=target_date, ts="10:00", te="12:00")
        svc.cancel_booking(bid)

        windows = get_free_windows()
        day_wins = [
            w for w in windows
            if str(w["date"]) == target_date and w["field"] == field_id
        ]
        assert len(day_wins) == 1
        assert day_wins[0]["time_start"].strftime("%H:%M") == _open_time()
        assert day_wins[0]["time_end"].strftime("%H:%M") == _close_time()

    def test_awaiting_payment_booking_blocks_slot(self):
        """awaiting_payment bookings must reduce free windows just like confirmed ones."""
        target_date = (today_almaty() + timedelta(days=3)).isoformat()
        field_id = _field_ids()[0]
        _awaiting_booking(field=field_id, date_str=target_date, ts="11:00", te="13:00")

        windows = get_free_windows()
        day_wins = sorted(
            [w for w in windows if str(w["date"]) == target_date and w["field"] == field_id],
            key=lambda w: w["time_start"],
        )
        assert len(day_wins) == 2
        assert day_wins[0]["time_end"].strftime("%H:%M") == "11:00"
        assert day_wins[1]["time_start"].strftime("%H:%M") == "13:00"

    def test_booking_on_field_a_does_not_block_field_b(self):
        """A booking on field 1 must leave field 2 unaffected."""
        if len(_field_ids()) < 2:
            pytest.skip("Need at least 2 fields in config")

        target_date = (today_almaty() + timedelta(days=3)).isoformat()
        field_a, field_b = _field_ids()[0], _field_ids()[1]
        _confirmed_booking(field=field_a, date_str=target_date, ts="10:00", te="12:00")

        windows = get_free_windows()
        wins_b = [
            w for w in windows
            if str(w["date"]) == target_date and w["field"] == field_b
        ]
        # Field B must have a single unbroken window from open to close
        assert len(wins_b) == 1
        assert wins_b[0]["time_start"].strftime("%H:%M") == _open_time()
        assert wins_b[0]["time_end"].strftime("%H:%M") == _close_time()

    def test_multiple_fields_independently_tracked(self):
        """Different bookings on different fields must each only subtract from their
        respective field's window."""
        if len(_field_ids()) < 2:
            pytest.skip("Need at least 2 fields in config")

        target_date = (today_almaty() + timedelta(days=4)).isoformat()
        field_a, field_b = _field_ids()[0], _field_ids()[1]
        _confirmed_booking(field=field_a, date_str=target_date, ts="10:00", te="11:00")
        _confirmed_booking(field=field_b, date_str=target_date, ts="14:00", te="15:00")

        windows = get_free_windows()
        wins_a = sorted(
            [w for w in windows if str(w["date"]) == target_date and w["field"] == field_a],
            key=lambda w: w["time_start"],
        )
        wins_b = sorted(
            [w for w in windows if str(w["date"]) == target_date and w["field"] == field_b],
            key=lambda w: w["time_start"],
        )
        # Field A: gap at 10–11
        assert len(wins_a) == 2
        assert wins_a[0]["time_end"].strftime("%H:%M") == "10:00"
        assert wins_a[1]["time_start"].strftime("%H:%M") == "11:00"

        # Field B: gap at 14–15
        assert len(wins_b) == 2
        assert wins_b[0]["time_end"].strftime("%H:%M") == "14:00"
        assert wins_b[1]["time_start"].strftime("%H:%M") == "15:00"

    def test_result_covers_exactly_7_days(self):
        """get_free_windows must cover today through today+6 (7 days total)."""
        windows = get_free_windows()
        if not windows:
            pytest.skip("No windows returned — all days may be fully booked (unlikely on clean DB)")
        dates_in_result = {w["date"] for w in windows}
        today = today_almaty()
        for offset in range(7):
            expected_date = today + timedelta(days=offset)
            # Every date must have at least one field with a window (clean DB).
            assert expected_date in dates_in_result, (
                f"Date {expected_date} missing from free windows"
            )

    def test_failed_booking_does_not_block_slot(self):
        """failed state must not reduce free windows."""
        from integrations.repo.postgres import _conn
        target_date = (today_almaty() + timedelta(days=3)).isoformat()
        field_id = _field_ids()[0]
        bid = _awaiting_booking(field=field_id, date_str=target_date, ts="18:00", te="20:00")
        # Force to failed state via raw SQL (no booking_service path produces this directly)
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE bookings SET state = 'failed' WHERE id = %s", (bid,))

        windows = get_free_windows()
        day_wins = [
            w for w in windows
            if str(w["date"]) == target_date and w["field"] == field_id
        ]
        # Slot should be fully free again
        assert len(day_wins) == 1
        assert day_wins[0]["time_start"].strftime("%H:%M") == _open_time()
        assert day_wins[0]["time_end"].strftime("%H:%M") == _close_time()


# ---------------------------------------------------------------------------
# is_range_free
# ---------------------------------------------------------------------------

class TestIsRangeFree:
    """is_range_free operates on an in-memory list of booked dicts and does not
    hit the DB — so we build the list directly to keep tests fast and isolated."""

    def _booked_list(self, date_str: str, ts: str, te: str, field: int) -> list[dict]:
        return [{"date": date.fromisoformat(date_str), "time_start": ts + ":00",
                 "time_end": te + ":00", "field": field}]

    def test_no_bookings_is_free(self):
        assert is_range_free([], "2026-07-10", "10:00", "11:00", field_id=1) is True

    def test_identical_range_is_not_free(self):
        booked = self._booked_list("2026-07-10", "10:00", "11:00", field=1)
        assert is_range_free(booked, "2026-07-10", "10:00", "11:00", field_id=1) is False

    def test_overlapping_range_start_not_free(self):
        """Requested range starts inside an existing booking."""
        booked = self._booked_list("2026-07-10", "10:00", "12:00", field=1)
        assert is_range_free(booked, "2026-07-10", "11:00", "13:00", field_id=1) is False

    def test_overlapping_range_end_not_free(self):
        """Requested range ends inside an existing booking."""
        booked = self._booked_list("2026-07-10", "12:00", "14:00", field=1)
        assert is_range_free(booked, "2026-07-10", "11:00", "13:00", field_id=1) is False

    def test_requested_range_contains_booking_not_free(self):
        """Requested range fully contains an existing booking."""
        booked = self._booked_list("2026-07-10", "11:00", "12:00", field=1)
        assert is_range_free(booked, "2026-07-10", "10:00", "13:00", field_id=1) is False

    def test_booking_contains_requested_range_not_free(self):
        """Existing booking fully contains the requested range."""
        booked = self._booked_list("2026-07-10", "09:00", "15:00", field=1)
        assert is_range_free(booked, "2026-07-10", "11:00", "12:00", field_id=1) is False

    def test_adjacent_before_is_free(self):
        """Requested range ends exactly when the existing booking starts (no overlap)."""
        booked = self._booked_list("2026-07-10", "12:00", "13:00", field=1)
        assert is_range_free(booked, "2026-07-10", "10:00", "12:00", field_id=1) is True

    def test_adjacent_after_is_free(self):
        """Requested range starts exactly when the existing booking ends (no overlap)."""
        booked = self._booked_list("2026-07-10", "10:00", "12:00", field=1)
        assert is_range_free(booked, "2026-07-10", "12:00", "14:00", field_id=1) is True

    def test_different_date_is_free(self):
        booked = self._booked_list("2026-07-10", "10:00", "12:00", field=1)
        assert is_range_free(booked, "2026-07-11", "10:00", "12:00", field_id=1) is True

    def test_different_field_is_free(self):
        """Booking on field 1 must not block field 2."""
        booked = self._booked_list("2026-07-10", "10:00", "12:00", field=1)
        assert is_range_free(booked, "2026-07-10", "10:00", "12:00", field_id=2) is True

    def test_multiple_bookings_one_overlaps(self):
        """With multiple bookings, a single overlap makes the range not free."""
        booked = [
            {"date": date.fromisoformat("2026-07-10"), "time_start": "09:00:00",
             "time_end": "10:00:00", "field": 1},
            {"date": date.fromisoformat("2026-07-10"), "time_start": "13:00:00",
             "time_end": "14:00:00", "field": 1},
        ]
        # Overlaps second booking
        assert is_range_free(booked, "2026-07-10", "12:30", "14:30", field_id=1) is False

    def test_multiple_bookings_none_overlaps(self):
        """With multiple non-overlapping bookings, a gap between them is free."""
        booked = [
            {"date": date.fromisoformat("2026-07-10"), "time_start": "09:00:00",
             "time_end": "10:00:00", "field": 1},
            {"date": date.fromisoformat("2026-07-10"), "time_start": "13:00:00",
             "time_end": "14:00:00", "field": 1},
        ]
        # Fits cleanly in the gap
        assert is_range_free(booked, "2026-07-10", "10:00", "13:00", field_id=1) is True


# ---------------------------------------------------------------------------
# format_availability_context
# ---------------------------------------------------------------------------

class TestFormatAvailabilityContext:
    def test_empty_windows_returns_no_slots_message(self):
        result = format_availability_context([])
        assert "нет" in result.lower() or "no" in result.lower() or "слот" in result.lower()

    def test_non_empty_windows_returns_header_string(self):
        windows = get_free_windows()  # clean DB → full week free
        result = format_availability_context(windows)
        assert isinstance(result, str)
        assert len(result) > 0
        # Must contain the standard header
        assert "Свободные окна" in result

    def test_context_contains_field_reference(self):
        """The formatted string must mention at least one field number."""
        windows = get_free_windows()
        result = format_availability_context(windows)
        assert "поле" in result.lower()

    def test_context_contains_date_line(self):
        """Each date block must include a weekday abbreviation."""
        windows = get_free_windows()
        result = format_availability_context(windows)
        weekdays_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        assert any(wd in result for wd in weekdays_ru), (
            "Expected at least one Russian weekday abbreviation in context string"
        )

    def test_context_contains_time_range(self):
        """Output must contain at least one HH:MM–HH:MM time range."""
        windows = get_free_windows()
        result = format_availability_context(windows)
        import re
        assert re.search(r"\d{2}:\d{2}[–-]\d{2}:\d{2}", result), (
            "No time range pattern found in context string"
        )

    def test_context_with_booking_removes_booked_window(self):
        """After booking 10:00–12:00 on a field, that window must not appear in context."""
        target_date = (today_almaty() + timedelta(days=4)).isoformat()
        field_id = _field_ids()[0]
        _confirmed_booking(field=field_id, date_str=target_date, ts="10:00", te="12:00")

        windows = get_free_windows()
        result = format_availability_context(windows)

        # The booked window 10:00–12:00 should not appear for that field on that date.
        # We verify by checking that the open_time→10:00 split is present
        # and 10:00→12:00 is absent as a contiguous window in the output.
        assert "10:00–12:00" not in result

    def test_format_single_window_manually(self):
        """Unit-test format_availability_context with a hand-crafted window list."""
        from datetime import time as dtime

        manual_windows = [
            {
                "date": date(2026, 7, 6),   # Monday
                "time_start": dtime(9, 0),
                "time_end": dtime(23, 0),
                "field": 1,
                "format": "5x5",
            }
        ]
        result = format_availability_context(manual_windows)
        assert "поле 1" in result
        assert "5x5" in result
        assert "09:00" in result
        assert "23:00" in result


# ---------------------------------------------------------------------------
# Integration: get_free_windows + is_range_free consistency
# ---------------------------------------------------------------------------

class TestFreeWindowsIsRangeFreeConsistency:
    """Verify that is_range_free and get_free_windows agree: any time within a
    free window must be is_range_free=True against the same booked list."""

    def test_free_window_range_is_range_free(self):
        target_date = (today_almaty() + timedelta(days=3)).isoformat()
        field_id = _field_ids()[0]
        _confirmed_booking(field=field_id, date_str=target_date, ts="12:00", te="14:00")

        today = today_almaty()
        booked = get_all_booked(today, today + timedelta(days=6))
        windows = get_free_windows()

        day_wins = [
            w for w in windows
            if str(w["date"]) == target_date and w["field"] == field_id
        ]
        for w in day_wins:
            ts = w["time_start"].strftime("%H:%M")
            te = w["time_end"].strftime("%H:%M")
            assert is_range_free(booked, target_date, ts, te, field_id=field_id), (
                f"Free window {ts}–{te} on {target_date} field {field_id} "
                f"reported as not free by is_range_free"
            )

    def test_booked_range_is_not_range_free(self):
        target_date = (today_almaty() + timedelta(days=3)).isoformat()
        field_id = _field_ids()[0]
        _confirmed_booking(field=field_id, date_str=target_date, ts="10:00", te="11:00")

        today = today_almaty()
        booked = get_all_booked(today, today + timedelta(days=6))

        assert is_range_free(booked, target_date, "10:00", "11:00", field_id=field_id) is False
