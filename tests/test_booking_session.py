"""Tests for handlers/booking_session.py — deterministic 6-step booking state machine.

Exercises the real Postgres-backed state machine end-to-end.
WhatsApp / OpenAI / Sheets calls are either no-op (GOOGLE_SPREADSHEET_ID empty)
or patched where required.

Run:
    POSTGRES_DSN=postgresql://dopshy:changeme@localhost:5432/dopshy poetry run pytest tests/test_booking_session.py
"""

import uuid
from datetime import date, timedelta
from unittest.mock import patch


import config
from handlers.sessions.booking_session import (
    handle_booking_turn,
    start_booking_flow,
    BookingPromptBuilder
)

from integrations import booking_service
from integrations.repo import postgres as svc
from integrations.repo.postgres import _conn, get_active_session

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PHONE_NUMBER_ID = "test-phone-id"
SENDER_PHONE = "77001234567"


def _chat_id() -> str:
    return f"test:{uuid.uuid4()}"


def _booking_state(booking_id: int) -> str:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM bookings WHERE id = %s", (booking_id,))
            return cur.fetchone()[0]


def _get_draft_fields(booking_id: int) -> dict:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT date, time_start, time_end, field, players, customer_name "
                "FROM bookings WHERE id = %s",
                (booking_id,),
            )
            row = cur.fetchone()
            return {
                "date": row[0],
                "time_start": row[1],
                "time_end": row[2],
                "field": row[3],
                "players": row[4],
                "customer_name": row[5],
            }


def _future_date_str(days_ahead: int = 3) -> str:
    """Return a date string far enough in the future that it won't be 'today'."""
    from utils import today_almaty
    return str(today_almaty() + timedelta(days=days_ahead))


def _seed_confirmed_booking(field_id: int, date_str: str, ts: str, te: str) -> int:
    """Insert a confirmed booking to occupy a slot (so SLOT_TAKEN fires)."""
    res = booking_service.manager_create_booking(
        field=field_id, date=date_str, end_date=date_str,
        time_start=ts, time_end=te,
        customer="Blocker", phone="7700000000",
    )
    assert res["ok"], f"seed failed: {res}"
    return res["data"]["booking_id"]


def _start_flow(chat_id: str) -> str:
    """Call start_booking_flow, patching get_free_windows to return at least one day."""
    from utils import today_almaty
    future = today_almaty() + timedelta(days=3)
    fake_windows = [
        {
            "date": future,
            "time_start": __import__("datetime").time(10, 0),
            "time_end": __import__("datetime").time(22, 0),
            "field": 1,
            "format": "5x5",
        },
        {
            "date": future,
            "time_start": __import__("datetime").time(10, 0),
            "time_end": __import__("datetime").time(22, 0),
            "field": 2,
            "format": "6x6",
        },
    ]
    with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake_windows):
        reply = start_booking_flow(chat_id, SENDER_PHONE)
    return reply


def _fake_windows_for_date(target_date: date):
    import datetime as dt
    return [
        {
            "date": target_date,
            "time_start": dt.time(10, 0),
            "time_end": dt.time(22, 0),
            "field": 1,
            "format": "5x5",
        },
        {
            "date": target_date,
            "time_start": dt.time(10, 0),
            "time_end": dt.time(22, 0),
            "field": 2,
            "format": "6x6",
        },
    ]


# ---------------------------------------------------------------------------
# _TIME_RANGE_RE — unit tests (no DB needed)
# ---------------------------------------------------------------------------

class TestTimeRangeRegex:
    def __init__(self):
        self.builder = BookingPromptBuilder('dopsy_bot')

    def test_standard_до(self):
        m = self.builder.TIME_RANGE_RE.search("10:00 до 12:00")
        assert m is not None
        assert m.group(1) == "10:00"
        assert m.group(2) == "12:00"

    def test_hyphen_separator(self):
        m = self.builder.TIME_RANGE_RE.search("14:30-16:00")
        assert m is not None
        assert m.group(1) == "14:30"
        assert m.group(2) == "16:00"

    def test_en_dash_separator(self):
        m = self.builder.TIME_RANGE_RE.search("09:00–11:00")
        assert m is not None
        assert m.group(1) == "09:00"
        assert m.group(2) == "11:00"

    def test_em_dash_separator(self):
        m = self.builder.TIME_RANGE_RE.search("18:00—20:00")
        assert m is not None
        assert m.group(1) == "18:00"
        assert m.group(2) == "20:00"

    def test_single_digit_hour(self):
        m = self.builder.TIME_RANGE_RE.search("9:00 до 11:00")
        assert m is not None
        assert m.group(1) == "9:00"
        assert m.group(2) == "11:00"

    def test_extra_whitespace(self):
        m = self.builder.TIME_RANGE_RE.search("10:00  до  12:00")
        assert m is not None

    def test_embedded_in_sentence(self):
        m = self.builder.TIME_RANGE_RE.search("Хочу забронировать с 10:00 до 12:00 завтра")
        assert m is not None
        assert m.group(1) == "10:00"
        assert m.group(2) == "12:00"

    def test_no_match_plain_text(self):
        assert self.builder.TIME_RANGE_RE.search("abc") is None

    def test_no_match_single_time(self):
        assert self.builder.TIME_RANGE_RE.search("10:00") is None

    def test_no_match_invalid_format(self):
        assert self.builder.TIME_RANGE_RE.search("1000 до 1200") is None


class TestPadTime:
    def __init__(self):
        self.builder = BookingPromptBuilder('dopsy_bot')

    def test_already_padded(self):
        assert self.builder.pad_time("10:00") == "10:00"

    def test_single_digit_hour(self):
        assert self.builder.pad_time("9:30") == "09:30"

    def test_midnight(self):
        assert self.builder.pad_time("0:00") == "00:00"


class TestIsCancelIntent:
    def __init__(self):
        self.builder = BookingPromptBuilder('dopsy_bot')

    def test_отмена(self):
        assert self.builder.is_cancel_intent("отмена")

    def test_передумал(self):
        assert self.builder.is_cancel_intent("передумал")

    def test_не_хочу(self):
        assert self.builder.is_cancel_intent("не хочу")

    def test_стоп(self):
        assert self.builder.is_cancel_intent("стоп")

    def test_тоқтат(self):
        assert self.builder.is_cancel_intent("тоқтат")

    def test_normal_да_not_cancel(self):
        assert not self.builder.is_cancel_intent("да")

    def test_normal_нет_not_cancel(self):
        assert not self.builder.is_cancel_intent("нет")

    def test_empty_string(self):
        assert not self.builder.is_cancel_intent("")


# ---------------------------------------------------------------------------
# step_time via handle_booking_turn — DB-backed
# ---------------------------------------------------------------------------

class TestStepTimeHandling:
    """Drive a session to step_time and poke the time-parsing branch."""

    def _reach_step_time(self, chat_id: str, target_date: date) -> None:
        """Create a session already in step_time for the given date."""
        fake = _fake_windows_for_date(target_date)
        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            reply = start_booking_flow(chat_id, SENDER_PHONE)
        assert "выберите дату" in reply.lower() or "1." in reply

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            reply2 = handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "1")
        assert reply2 is not None
        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        assert session["state"] == "step_time", f"expected step_time, got {session['state']}"

    def test_valid_time_range_advances_session(self):
        from utils import today_almaty
        chat_id = _chat_id()
        target = today_almaty() + timedelta(days=3)
        fake = _fake_windows_for_date(target)

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            start_booking_flow(chat_id, SENDER_PHONE)
            handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "1")

        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        assert session["state"] == "step_time"
        booking_id = session["params"]["booking_id"]

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            with patch("handlers.booking_session.booking_logic.get_all_booked", return_value=[]):
                reply = handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "10:00 до 12:00")

        assert reply is not None
        session_after = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        assert session_after is not None
        assert session_after["state"] in ("step_players", "step_field")

        fields = _get_draft_fields(booking_id)
        assert str(fields["time_start"])[:5] == "10:00"
        assert str(fields["time_end"])[:5] == "12:00"

    def test_valid_time_hyphen_separator(self):
        from utils import today_almaty
        chat_id = _chat_id()
        target = today_almaty() + timedelta(days=3)
        fake = _fake_windows_for_date(target)

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            start_booking_flow(chat_id, SENDER_PHONE)
            handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "1")

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            with patch("handlers.booking_session.booking_logic.get_all_booked", return_value=[]):
                reply = handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "14:30-16:00")

        assert reply is not None
        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        assert session["state"] in ("step_players", "step_field")

    def test_invalid_time_plain_text_stays_on_step_time(self):
        from utils import today_almaty
        chat_id = _chat_id()
        target = today_almaty() + timedelta(days=3)
        fake = _fake_windows_for_date(target)

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            start_booking_flow(chat_id, SENDER_PHONE)
            handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "1")

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            reply = handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "abc")

        assert reply is not None
        assert "не распознал" in reply.lower() or "пример" in reply.lower() or "10:00" in reply
        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        assert session["state"] == "step_time"

    def test_invalid_time_no_range_stays_on_step_time(self):
        from utils import today_almaty
        chat_id = _chat_id()
        target = today_almaty() + timedelta(days=3)
        fake = _fake_windows_for_date(target)

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            start_booking_flow(chat_id, SENDER_PHONE)
            handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "1")

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "просто текст без времени")

        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        assert session["state"] == "step_time"

    def test_no_free_fields_for_occupied_time_stays_on_step_time(self):
        """When the time parses but no field is free, session stays on step_time."""
        from utils import today_almaty
        chat_id = _chat_id()
        target = today_almaty() + timedelta(days=3)
        date_str = str(target)
        fake = _fake_windows_for_date(target)

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            start_booking_flow(chat_id, SENDER_PHONE)
            handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "1")

        for field_conf in config.BOOKING_FIELDS:
            _seed_confirmed_booking(field_conf["id"], date_str, "10:00", "12:00")

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            reply = handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "10:00 до 12:00")

        assert "нет свободных" in reply.lower() or "свободн" in reply.lower()
        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        assert session["state"] == "step_time"


# ---------------------------------------------------------------------------
# Full happy-path walk through all six steps
# ---------------------------------------------------------------------------

class TestFullHappyPath:
    def test_six_step_flow_ends_in_awaiting_payment(self):
        from utils import today_almaty

        chat_id = _chat_id()
        target = today_almaty() + timedelta(days=4)
        fake = _fake_windows_for_date(target)

        # ── step_date: start the flow ────────────────────────────────────────
        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            reply = start_booking_flow(chat_id, SENDER_PHONE)
        assert "выберите дату" in reply.lower() or "1." in reply

        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        assert session["state"] == "step_date"
        booking_id = session["params"]["booking_id"]
        assert _booking_state(booking_id) == "draft"

        # ── step_date → step_time ────────────────────────────────────────────
        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            reply = handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "1")
        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        assert session["state"] == "step_time"
        assert _get_draft_fields(booking_id)["date"] is not None

        # ── step_time → step_players (single field free) or step_field ───────
        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            with patch("handlers.booking_session.booking_logic.get_all_booked", return_value=[]):
                reply = handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "11:00 до 13:00")

        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        assert session is not None

        # If step_field was triggered (multiple free fields), pick one
        if session["state"] == "step_field":
            with patch("handlers.booking_session.booking_logic.get_all_booked", return_value=[]):
                reply = handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "1")
            session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)

        assert session["state"] == "step_players"
        draft = _get_draft_fields(booking_id)
        assert str(draft["time_start"])[:5] == "11:00"
        assert str(draft["time_end"])[:5] == "13:00"
        assert draft["field"] is not None

        # ── step_players → step_name ─────────────────────────────────────────
        reply = handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "10")
        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        assert session["state"] == "step_name"
        assert "имя" in reply.lower() or "укажите" in reply.lower()
        assert _get_draft_fields(booking_id)["players"] == 10

        # ── step_name → step_confirm ─────────────────────────────────────────
        reply = handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "Алибек Джаксыбеков")
        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        assert session["state"] == "step_confirm"
        assert "подтвердить" in reply.lower() or "детали" in reply.lower()
        assert _get_draft_fields(booking_id)["customer_name"] == "Алибек Джаксыбеков"

        # ── step_confirm → awaiting_payment ──────────────────────────────────
        # Patch Sheets so the background thread doesn't attempt a real API call
        with patch("handlers.booking_session.sheets.upsert_booking_row"):
            reply = handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "да")

        assert reply is not None
        assert "оплат" in reply.lower() or "kaspi" in reply.lower() or "бронь" in reply.lower()

        assert get_active_session(bot_name='dopsy_bot', chat_id=chat_id) is None
        assert _booking_state(booking_id) == "awaiting_payment"


# ---------------------------------------------------------------------------
# SLOT_TAKEN branch
# ---------------------------------------------------------------------------

class TestSlotTakenBranch:
    def test_slot_taken_returns_error_message_and_session_cleared(self):
        from utils import today_almaty
        import datetime as dt

        chat_id = _chat_id()
        target = today_almaty() + timedelta(days=5)
        date_str = str(target)
        field_id = 1

        fake = [
            {
                "date": target,
                "time_start": dt.time(10, 0),
                "time_end": dt.time(22, 0),
                "field": field_id,
                "format": "5x5",
            }
        ]

        # Pre-occupy the slot
        _seed_confirmed_booking(field_id, date_str, "14:00", "16:00")

        # Start a new flow for the same slot
        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            start_booking_flow(chat_id, SENDER_PHONE)

        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        booking_id = session["params"]["booking_id"]

        # Manually populate the DRAFT so request_payment can be called
        svc.update_draft(bot_name='dopsy_bot', object_id=booking_id, date=date_str, time_start="14:00", time_end="16:00",
                         field=field_id, format="5x5", players=8, customer_name="Racer")

        # Advance session to step_confirm by writing it directly
        params = session["params"].copy()
        params.update({
            "date": date_str,
            "time_start": "14:00",
            "time_end": "16:00",
            "field": field_id,
            "format": "5x5",
            "players": 8,
            "customer_name": "Racer",
        })
        svc.upsert_session(bot_name='dopsy_bot', chat_id=chat_id, state="step_confirm", params=params, object_id=booking_id)

        # Confirm — must hit SLOT_TAKEN
        with patch("handlers.booking_session.sheets.upsert_booking_row"):
            with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
                reply = handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "да")

        assert reply is not None
        assert "слот" in reply.lower() or "занят" in reply.lower() or "заняли" in reply.lower()

        # Session must be cleared
        assert get_active_session(bot_name='dopsy_bot', chat_id=chat_id) is None

        # DRAFT booking must NOT be in awaiting_payment
        state = _booking_state(booking_id)
        assert state != "awaiting_payment", f"expected non-awaiting_payment, got {state}"

    def test_slot_taken_draft_state_unchanged(self):
        """The DRAFT row stays in 'draft' state when SLOT_TAKEN fires (not cancelled/failed)."""
        from utils import today_almaty
        import datetime as dt

        chat_id = _chat_id()
        target = today_almaty() + timedelta(days=5)
        date_str = str(target)
        field_id = 2

        fake = [
            {
                "date": target,
                "time_start": dt.time(10, 0),
                "time_end": dt.time(22, 0),
                "field": field_id,
                "format": "6x6",
            }
        ]

        _seed_confirmed_booking(field_id, date_str, "16:00", "18:00")

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            start_booking_flow(chat_id, SENDER_PHONE)

        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        booking_id = session["params"]["booking_id"]

        svc.update_draft('dopsy_bot', booking_id, date=date_str, time_start="16:00", time_end="18:00",
                         field=field_id, format="6x6", players=6, customer_name="Test2")

        from integrations.repo.postgres import upsert_session
        params = session["params"].copy()
        params.update({
            "date": date_str, "time_start": "16:00", "time_end": "18:00",
            "field": field_id, "format": "6x6", "players": 6, "customer_name": "Test2",
        })
        upsert_session(bot_name='dopsy_bot', chat_id=chat_id, state="step_confirm", params=params, object_id=booking_id)

        with patch("handlers.booking_session.sheets.upsert_booking_row"):
            with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
                handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "да")

        assert _booking_state(booking_id) == "draft"


# ---------------------------------------------------------------------------
# Mid-flow cancel
# ---------------------------------------------------------------------------

class TestMidFlowCancel:
    def _start_and_advance_to_step_time(self, chat_id: str, target: date):
        fake = _fake_windows_for_date(target)
        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            start_booking_flow(chat_id, SENDER_PHONE)
            handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "1")
        return get_active_session(bot_name='dopsy_bot', chat_id=chat_id)

    def test_отмена_at_step_time_cancels_draft_and_clears_session(self):
        from utils import today_almaty
        chat_id = _chat_id()
        target = today_almaty() + timedelta(days=3)

        session = self._start_and_advance_to_step_time(chat_id, target)
        assert session["state"] == "step_time"
        booking_id = session["params"]["booking_id"]

        fake = _fake_windows_for_date(target)
        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            reply = handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "отмена")

        assert reply is not None
        assert "отменена" in reply.lower() or "брондау" in reply.lower()
        assert get_active_session(bot_name='dopsy_bot', chat_id=chat_id) is None
        assert _booking_state(booking_id) == "cancelled"

    def test_передумал_at_step_players_cancels(self):
        from utils import today_almaty
        chat_id = _chat_id()
        target = today_almaty() + timedelta(days=3)
        fake = _fake_windows_for_date(target)

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            start_booking_flow(chat_id, SENDER_PHONE)
            handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "1")

        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        booking_id = session["params"]["booking_id"]

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            with patch("handlers.booking_session.booking_logic.get_all_booked", return_value=[]):
                handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "11:00 до 13:00")

        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        if session and session["state"] == "step_field":
            with patch("handlers.booking_session.booking_logic.get_all_booked", return_value=[]):
                handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "1")

        reply = handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "передумал")
        assert reply is not None
        assert get_active_session(bot_name='dopsy_bot', chat_id=chat_id) is None
        assert _booking_state(booking_id) == "cancelled"

    def test_cancel_at_step_confirm_via_нет(self):
        """Responding 'нет' at step_confirm is a graceful NO, not a cancel-intent cancel."""
        from utils import today_almaty
        chat_id = _chat_id()
        target = today_almaty() + timedelta(days=4)
        date_str = str(target)
        fake = _fake_windows_for_date(target)
        field_id = 1

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            start_booking_flow(chat_id, SENDER_PHONE)

        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        booking_id = session["params"]["booking_id"]

        svc.update_draft('dopsy_bot', booking_id, date=date_str, time_start="10:00", time_end="11:00",
                         field=field_id, format="5x5", players=8, customer_name="Тест")
        from integrations.repo.postgres import upsert_session
        params = session["params"].copy()
        params.update({
            "date": date_str, "time_start": "10:00", "time_end": "11:00",
            "field": field_id, "format": "5x5", "players": 8, "customer_name": "Тест",
        })
        upsert_session(bot_name='dopsy_bot', chat_id=chat_id, state="step_confirm", params=params, object_id=booking_id)

        reply = handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "нет")
        assert reply is not None
        assert "отменено" in reply.lower() or "отменена" in reply.lower() or "захотите" in reply.lower()
        assert get_active_session(bot_name='dopsy_bot', chat_id=chat_id) is None
        assert _booking_state(booking_id) == "cancelled"

    def test_не_хочу_at_step_date(self):
        from utils import today_almaty
        chat_id = _chat_id()
        target = today_almaty() + timedelta(days=3)
        fake = _fake_windows_for_date(target)

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            start_booking_flow(chat_id, SENDER_PHONE)

        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        booking_id = session["params"]["booking_id"]

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            reply = handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "не хочу")

        assert reply is not None
        assert get_active_session(bot_name='dopsy_bot', chat_id=chat_id) is None
        assert _booking_state(booking_id) == "cancelled"


# ---------------------------------------------------------------------------
# start_booking_flow edge cases
# ---------------------------------------------------------------------------

class TestStartBookingFlow:
    def test_no_free_windows_returns_error_message(self):
        chat_id = _chat_id()
        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=[]):
            reply = start_booking_flow(chat_id, SENDER_PHONE)
        assert "нет" in reply.lower() or "нет." in reply.lower()
        assert get_active_session(bot_name='dopsy_bot', chat_id=chat_id) is None

    def test_creates_draft_booking_and_session(self):
        from utils import today_almaty
        chat_id = _chat_id()
        target = today_almaty() + timedelta(days=2)
        fake = _fake_windows_for_date(target)

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            reply = start_booking_flow(chat_id, SENDER_PHONE)

        assert reply is not None
        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        assert session is not None
        assert session["state"] == "step_date"
        booking_id = session["params"]["booking_id"]
        assert _booking_state(booking_id) == "draft"


# ---------------------------------------------------------------------------
# step_date — date-picker parsing
# ---------------------------------------------------------------------------

class TestStepDate:
    def test_numeric_choice_selects_date(self):
        from utils import today_almaty
        chat_id = _chat_id()
        target = today_almaty() + timedelta(days=3)
        fake = _fake_windows_for_date(target)

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            start_booking_flow(chat_id, SENDER_PHONE)
            reply = handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "1")

        assert reply is not None
        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        assert session["state"] == "step_time"

    def test_invalid_choice_stays_on_step_date(self):
        from utils import today_almaty
        chat_id = _chat_id()
        target = today_almaty() + timedelta(days=3)
        fake = _fake_windows_for_date(target)

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            start_booking_flow(chat_id, SENDER_PHONE)
            reply = handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "99")

        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        assert session["state"] == "step_date"
        assert "список" in reply.lower() or "введите" in reply.lower() or "номер" in reply.lower()


# ---------------------------------------------------------------------------
# step_players — integer parsing
# ---------------------------------------------------------------------------

class TestStepPlayers:
    def _reach_step_players(self, chat_id: str, target: date) -> int:
        fake = _fake_windows_for_date(target)
        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            start_booking_flow(chat_id, SENDER_PHONE)
            handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "1")

        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        booking_id = session["params"]["booking_id"]

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            with patch("handlers.booking_session.booking_logic.get_all_booked", return_value=[]):
                handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "11:00 до 13:00")

        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        if session and session["state"] == "step_field":
            with patch("handlers.booking_session.booking_logic.get_all_booked", return_value=[]):
                handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "1")

        return booking_id

    def test_valid_player_count_advances(self):
        from utils import today_almaty
        chat_id = _chat_id()
        target = today_almaty() + timedelta(days=3)
        bid = self._reach_step_players(chat_id, target)
        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        assert session["state"] == "step_players"

        handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "8")
        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        assert session["state"] == "step_name"
        assert _get_draft_fields(bid)["players"] == 8

    def test_non_numeric_input_stays_on_step_players(self):
        from utils import today_almaty
        chat_id = _chat_id()
        target = today_almaty() + timedelta(days=3)
        self._reach_step_players(chat_id, target)
        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        assert session["state"] == "step_players"

        reply = handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "много")
        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        assert session["state"] == "step_players"
        assert "количество" in reply.lower() or "введите" in reply.lower()


# ---------------------------------------------------------------------------
# step_confirm — unrecognised response re-shows summary
# ---------------------------------------------------------------------------

class TestStepConfirm:
    def test_unrecognised_response_reshows_summary(self):
        from utils import today_almaty
        chat_id = _chat_id()
        target = today_almaty() + timedelta(days=4)
        date_str = str(target)
        fake = _fake_windows_for_date(target)
        field_id = 1

        with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
            start_booking_flow(chat_id, SENDER_PHONE)

        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        booking_id = session["params"]["booking_id"]

        svc.update_draft(bot_name='dopsy_bot', object_id=booking_id, date=date_str, time_start="10:00", time_end="11:00",
                         field=field_id, format="5x5", players=8, customer_name="Тест")
        from integrations.repo.postgres import upsert_session
        params = session["params"].copy()
        params.update({
            "date": date_str, "time_start": "10:00", "time_end": "11:00",
            "field": field_id, "format": "5x5", "players": 8, "customer_name": "Тест",
        })
        upsert_session(bot_name='dopsy_bot', chat_id=chat_id, state="step_confirm", params=params, object_id=booking_id)

        reply = handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, "может быть")
        session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
        assert session["state"] == "step_confirm"
        assert "да" in reply.lower() or "нет" in reply.lower() or "подтвердить" in reply.lower()

    def test_yes_variants_all_confirm(self):
        """Several yes-tokens all succeed."""
        from utils import today_almaty
        for idx, yes_word in enumerate(("да", "ok", "ок", "yes", "иә")):
            chat_id = _chat_id()
            target = today_almaty() + timedelta(days=4)
            date_str = str(target)
            fake = _fake_windows_for_date(target)
            field_id = 1
            start_hour = 10 + idx
            time_start = f"{start_hour:02d}:00"
            time_end = f"{start_hour + 1:02d}:00"

            with patch("handlers.booking_session.booking_logic.get_free_windows", return_value=fake):
                start_booking_flow(chat_id, SENDER_PHONE)

            session = get_active_session(bot_name='dopsy_bot', chat_id=chat_id)
            booking_id = session["params"]["booking_id"]

            svc.update_draft(bot_name='dopcy_bot', object_id=booking_id, date=date_str, time_start=time_start, time_end=time_end,
                             field=field_id, format="5x5", players=8, customer_name="T")
            from integrations.repo.postgres import upsert_session
            params = session["params"].copy()
            params.update({
                "date": date_str, "time_start": time_start, "time_end": time_end,
                "field": field_id, "format": "5x5", "players": 8, "customer_name": "T",
            })
            upsert_session(bot_name='dopsy_bot', chat_id=chat_id, state="step_confirm", params=params, object_id=booking_id)

            with patch("handlers.booking_session.sheets.upsert_booking_row"):
                handle_booking_turn(chat_id, PHONE_NUMBER_ID, SENDER_PHONE, yes_word)

            assert _booking_state(booking_id) == "awaiting_payment", \
                f"'{yes_word}' did not confirm — got state {_booking_state(booking_id)}"
