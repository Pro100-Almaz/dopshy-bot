# Non-deterministic booking flow — processes partially-filled booking data from the LLM.
#
# The standard deterministic flow (booking_session.py) walks users through steps
# one at a time: intent → date → time → field → players → name → confirm.
#
# But users often provide several details at once in a single message:
#   "хочу забронировать на четверг с 18:00 до 20:00 на поле 2"
#
# This module accepts whatever fields the LLM managed to extract, validates them,
# checks real-time availability against the database, creates a DRAFT booking,
# and sets up a session so the deterministic flow continues collecting any remaining info.
#
# Booking requires: date, time_start, time_end, field, players, customer_name
# (see migrations/ for the full bookings table schema).

import json
import logging
import uuid
from datetime import datetime, date, timedelta

import config
from integrations import booking as booking_logic
from integrations.repo import postgres
from utils import today_almaty

logger = logging.getLogger(__name__)

_WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_BOT_NAME = "dopsy_bot"


class LlmBookingFlowHandler:
    """
    Handles booking creation from partially-filled LLM-parsed data.

    The LLM extracts structured booking params as a JSON string:
        {"date": "2026-06-12", "time_start": "18:00", "time_end": "20:00",
         "field": 2, "players": 10, "name": "Алмаз"}

    All keys are optional. Based on which keys are present, the handler:
      1. Checks real-time availability and builds a user-facing response string
      2. Creates (or updates) a DRAFT booking with the known fields
      3. Saves a session so the step-by-step flow continues from the right point

    Checking logic (6 cases):
      Case 1 — date only              → free fields and time ranges for that date
      Case 2 — time_start + time_end  → available dates and fields for that interval
      Case 3 — field only             → dates and time ranges for that field
      Case 4 — date+time+field        → is this specific slot free?
      Case 5 — date+time (no field)   → which fields are available?
      Case 6 — only start OR only end → ask user for both
    """

    def handle(self, json_str: str, chat_id: str, sender_phone: str, lang: str = "ru") -> str:
        """
        Main entry point. Parses LLM output, validates, checks availability,
        creates/updates draft, and returns a response string for WhatsApp.

        Args:
            json_str:      JSON string (or dict) with booking params from the LLM.
            chat_id:       WhatsApp chat identifier (used for session + draft).
            sender_phone:  User's phone number (stored on the draft).
            lang:          Language code ("ru" or "kk").

        Returns:
            Human-readable response string to send to the client via WhatsApp.
        """
        # --- Parse LLM output (accept both raw string and pre-parsed dict) ---
        try:
            data = json.loads(json_str) if isinstance(json_str, str) else json_str
        except (json.JSONDecodeError, TypeError):
            logger.warning("[LLM_BOOKING] Failed to parse JSON: %.200s", json_str)
            return "Не удалось разобрать данные. Пожалуйста, уточните детали бронирования."

        # --- Validate each field individually ---
        date_str   = self._validate_date(data.get("date"))
        time_start = self._validate_time(data.get("time_start"))
        time_end   = self._validate_time(data.get("time_end"))
        field_id   = self._validate_field(data.get("field"))
        players    = self._validate_players(data.get("players"))
        name       = (data.get("name") or data.get("customer_name") or "").strip() or None

        logger.info(
            "[LLM_BOOKING] Parsed: date=%s time=%s-%s field=%s players=%s name=%s",
            date_str, time_start, time_end, field_id, players, name,
        )

        # --- Convenience flags ---
        has_date  = date_str is not None
        has_start = time_start is not None
        has_end   = time_end is not None
        has_both  = has_start and has_end
        has_field = field_id is not None

        # ── Case 6: only one of start/end → incomplete, ask for both ──────
        if has_start != has_end:
            return (
                "Пожалуйста, укажите и время начала, и время окончания.\n"
                "Например: *18:00 до 20:00*"
            )

        # ── Route to the matching availability check ──────────────────────
        if has_date and has_both and has_field:
            # Case 4: full slot — check this exact field at this exact time
            response, slot_ok = self._check_full_slot(date_str, time_start, time_end, field_id)
            if not slot_ok:
                # Slot is taken — don't persist the unavailable field in the draft
                field_id = None

        elif has_date and has_both:
            # Case 5: date + time range → which fields are free?
            response = self._check_date_and_time(date_str, time_start, time_end)
            # Auto-select field when only one is available (mirrors deterministic flow)
            free = _get_free_fields_for_slot(date_str, time_start, time_end)
            if len(free) == 1:
                field_id = free[0]["id"]

        elif has_date and not has_both and not has_field:
            # Case 1: date only → show all free windows for that day
            response = self._check_date_only(date_str)

        elif has_both and not has_date:
            # Case 2: time range only → show which dates have this slot free
            response = self._check_time_range_only(time_start, time_end)

        elif has_field and not has_date and not has_both:
            # Case 3: field only → show when this field is available
            response = self._check_field_only(field_id)

        elif has_date and has_field:
            # Bonus: date + field, no time → show free windows for that combo
            response = self._check_date_and_field(date_str, field_id)

        else:
            # Nothing usable extracted → show full 7-day availability
            response = self._show_general_availability()

        # ── Build draft fields from validated data ────────────────────────
        draft_fields = {}
        if date_str:
            draft_fields["date"] = date_str
        if time_start:
            draft_fields["time_start"] = time_start
        if time_end:
            draft_fields["time_end"] = time_end
        if field_id:
            draft_fields["field"] = field_id
            # Look up the format (5x5, 6x6) for this field from config
            fmt = next((f["format"] for f in config.BOOKING_FIELDS if f["id"] == field_id), None)
            if fmt:
                draft_fields["format"] = fmt
        if players:
            draft_fields["players"] = players
        if name:
            draft_fields["customer_name"] = name

        # ── Persist draft + session ───────────────────────────────────────
        self._create_or_update_draft(chat_id, sender_phone, lang, draft_fields)

        return response

    # ──────────────────────────────────────────────────────────────────────
    #  Availability checks (cases 1–6)
    #  Each method queries real-time booking data and returns a formatted
    #  string describing what's available.
    # ──────────────────────────────────────────────────────────────────────

    def _check_date_only(self, date_str: str) -> str:
        """
        Case 1: only date provided.
        Fetches free time windows for that day and groups them by field.
        Asks the user to specify a time range.
        """
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        free = booking_logic.get_free_windows()
        day_windows = [w for w in free if w["date"] == d]

        if not day_windows:
            return (
                f"К сожалению, на {_fmt_date(d)} нет свободных слотов.\n"
                "Попробуйте выбрать другую дату."
            )

        # Group windows by (field_id, format) for a clean per-field listing
        by_field: dict[tuple, list] = {}
        for w in day_windows:
            by_field.setdefault((w["field"], w["format"]), []).append(w)

        lines = [f"📅 {_fmt_date(d)} — свободные окна:\n"]
        for (fid, fmt) in sorted(by_field):
            ranges = ", ".join(
                f"{w['time_start'].strftime('%H:%M')}–{w['time_end'].strftime('%H:%M')}"
                for w in sorted(by_field[(fid, fmt)], key=lambda w: w["time_start"])
            )
            lines.append(f"  Поле {fid} ({fmt}): {ranges}")

        lines.append("\nУкажите время начала и окончания (например: *18:00 до 20:00*).")
        return "\n".join(lines)

    def _check_time_range_only(self, time_start: str, time_end: str) -> str:
        """
        Case 2: only time_start and time_end provided.
        First validates the range isn't inverted, then checks every day in the
        7-day booking window for fields free during that interval.
        Asks the user to pick a date.
        """
        # Guard: time_end must be after time_start
        if time_start >= time_end:
            return (
                "Время окончания должно быть позже времени начала.\n"
                "Например: *18:00 до 20:00*"
            )

        week_start, week_end = booking_logic.get_week_range()
        booked = booking_logic.get_all_booked(week_start, week_end)

        # Walk each day and collect fields where the requested range is free
        available: list[dict] = []
        current = week_start
        while current <= week_end:
            free_fields = [
                f for f in config.BOOKING_FIELDS
                if booking_logic.is_range_free(booked, str(current), time_start, time_end, f["id"])
            ]
            if free_fields:
                available.append({"date": current, "fields": free_fields})
            current += timedelta(days=1)

        if not available:
            return (
                f"К сожалению, на время {time_start}–{time_end} "
                f"нет свободных полей в ближайшие 7 дней.\n"
                "Попробуйте другое время."
            )

        lines = [f"⏰ {time_start}–{time_end} — доступные даты:\n"]
        for item in available:
            fields_str = ", ".join(
                f"Поле {f['id']} ({f['format']})" for f in item["fields"]
            )
            lines.append(f"  {_fmt_date(item['date'])}: {fields_str}")
        lines.append("\nКакую дату выбираете?")
        return "\n".join(lines)

    def _check_field_only(self, field_id: int) -> str:
        """
        Case 3: only field provided.
        Shows all dates and free time windows for this field in the next 7 days.
        Asks the user to specify a date and time.
        """
        field_conf = next((f for f in config.BOOKING_FIELDS if f["id"] == field_id), None)
        if not field_conf:
            return f"Поле {field_id} не найдено. Доступные поля: {_list_fields()}."

        free = booking_logic.get_free_windows()
        field_windows = [w for w in free if w["field"] == field_id]

        if not field_windows:
            return (
                f"Поле {field_id} ({field_conf['format']}) полностью занято "
                "на ближайшие 7 дней."
            )

        # Group by date for readability
        by_date: dict[date, list] = {}
        for w in field_windows:
            by_date.setdefault(w["date"], []).append(w)

        lines = [f"⚽ Поле {field_id} ({field_conf['format']}) — свободные окна:\n"]
        for d in sorted(by_date):
            ranges = ", ".join(
                f"{w['time_start'].strftime('%H:%M')}–{w['time_end'].strftime('%H:%M')}"
                for w in sorted(by_date[d], key=lambda w: w["time_start"])
            )
            lines.append(f"  {_fmt_date(d)}: {ranges}")
        lines.append("\nУкажите дату и время.")
        return "\n".join(lines)

    def _check_full_slot(
        self, date_str: str, time_start: str, time_end: str, field_id: int
    ) -> tuple[str, bool]:
        """
        Case 4: date + time_start + time_end + field — all slot params provided.
        Checks whether this exact slot is free. If not, suggests alternative fields.

        Returns:
            (response_string, is_slot_available)
            The bool tells the caller whether the requested field can be saved to the draft.
        """
        # Guard: inverted time range
        if time_start >= time_end:
            return (
                "Время окончания должно быть позже времени начала.\n"
                "Например: *18:00 до 20:00*"
            ), False

        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        field_conf = next((f for f in config.BOOKING_FIELDS if f["id"] == field_id), None)
        if not field_conf:
            return f"Поле {field_id} не найдено. Доступные поля: {_list_fields()}.", False

        week_start, week_end = booking_logic.get_week_range()
        booked = booking_logic.get_all_booked(week_start, week_end)

        # ── Requested slot is free ──
        if booking_logic.is_range_free(booked, date_str, time_start, time_end, field_id):
            return (
                f"✅ Поле {field_id} ({field_conf['format']}) свободно!\n"
                f"📅 {_fmt_date(d)}, ⏰ {time_start}–{time_end}\n\n"
                "Сколько игроков будет?"
            ), True

        # ── Slot is taken — check if other fields are free at this time ──
        alt_fields = [
            f for f in config.BOOKING_FIELDS
            if f["id"] != field_id
            and booking_logic.is_range_free(booked, date_str, time_start, time_end, f["id"])
        ]
        header = (
            f"❌ Поле {field_id} ({field_conf['format']}) занято "
            f"на {_fmt_date(d)} {time_start}–{time_end}."
        )
        if alt_fields:
            alts = ", ".join(f"Поле {f['id']} ({f['format']})" for f in alt_fields)
            return f"{header}\n\nСвободные поля на это время:\n  {alts}", False

        return f"{header}\nВсе поля заняты в это время. Попробуйте другую дату или время.", False

    def _check_date_and_time(self, date_str: str, time_start: str, time_end: str) -> str:
        """
        Case 5: date + time_start + time_end, but no field.
        Shows which fields are available for this date/time slot.
        If exactly one field is free, auto-selects it and asks for player count.
        """
        # Guard: inverted time range
        if time_start >= time_end:
            return (
                "Время окончания должно быть позже времени начала.\n"
                "Например: *18:00 до 20:00*"
            )

        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        free_fields = _get_free_fields_for_slot(date_str, time_start, time_end)

        if not free_fields:
            return (
                f"К сожалению, на {_fmt_date(d)} {time_start}–{time_end} "
                "нет свободных полей.\nПопробуйте другое время."
            )

        # Single free field — auto-select and skip field choice step
        if len(free_fields) == 1:
            f = free_fields[0]
            return (
                f"📅 {_fmt_date(d)}, ⏰ {time_start}–{time_end}\n"
                f"Поле {f['id']} ({f['format']}) — свободно ✅\n\n"
                "Сколько игроков будет?"
            )

        # Multiple free fields — ask user to pick one
        lines = [f"📅 {_fmt_date(d)}, ⏰ {time_start}–{time_end}\n", "Свободные поля:"]
        for f in free_fields:
            lines.append(f"  Поле {f['id']} ({f['format']}) ✅")
        lines.append("\nКакое поле выбираете?")
        return "\n".join(lines)

    def _check_date_and_field(self, date_str: str, field_id: int) -> str:
        """
        Bonus case: date + field provided, no time.
        Shows free time windows for this field on this date.
        Asks the user to specify a time range.
        """
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        field_conf = next((f for f in config.BOOKING_FIELDS if f["id"] == field_id), None)
        if not field_conf:
            return f"Поле {field_id} не найдено. Доступные поля: {_list_fields()}."

        free = booking_logic.get_free_windows()
        windows = sorted(
            [w for w in free if w["date"] == d and w["field"] == field_id],
            key=lambda w: w["time_start"],
        )

        if not windows:
            return (
                f"Поле {field_id} ({field_conf['format']}) полностью занято "
                f"на {_fmt_date(d)}."
            )

        ranges = ", ".join(
            f"{w['time_start'].strftime('%H:%M')}–{w['time_end'].strftime('%H:%M')}"
            for w in windows
        )
        return (
            f"📅 {_fmt_date(d)}, Поле {field_id} ({field_conf['format']})\n"
            f"Свободные окна: {ranges}\n\n"
            "Укажите время начала и окончания (например: *18:00 до 20:00*)."
        )

    @staticmethod
    def _show_general_availability() -> str:
        """Fallback: no recognized params — show full 7-day availability."""
        free = booking_logic.get_free_windows()
        if not free:
            return "К сожалению, свободных слотов на ближайшие 7 дней нет."
        return booking_logic.format_availability_context(free)

    # ──────────────────────────────────────────────────────────────────────
    #  Draft & session management
    #
    #  After checking availability we persist a DRAFT booking row and a
    #  booking_session row.  The session's `state` is set to the earliest
    #  step whose required data is still missing, so the deterministic
    #  flow (booking_session.py) picks up seamlessly from there.
    # ──────────────────────────────────────────────────────────────────────

    def _create_or_update_draft(
        self,
        chat_id: str,
        sender_phone: str,
        lang: str,
        fields: dict,
    ) -> int:
        """
        Create a new DRAFT booking or update the existing one for this chat.
        Also saves a session at the correct step so the deterministic flow
        can continue collecting missing info.

        Returns the booking_id.
        """
        session = postgres.get_active_session(_BOT_NAME, chat_id)

        if session and session.get("booking_id"):
            # ── Existing session — update draft with new fields ──
            booking_id = session["booking_id"]
            params = session.get("params") or {}
            if isinstance(params, str):
                params = json.loads(params)
            if fields:
                postgres.update_draft(_BOT_NAME, booking_id, **fields)
            params.update(fields)
        else:
            # ── No session — create a fresh draft ──
            client_token = str(uuid.uuid4())
            draft_data = {"phone": sender_phone, "client_token": client_token}
            draft_data.update(fields)
            result = postgres.create_draft(_BOT_NAME, chat_id, **draft_data)
            booking_id = result["data"]["booking_id"]
            params = {
                "sender_phone": sender_phone,
                "booking_id": booking_id,
                "client_token": client_token,
                "lang": lang,
            }
            params.update(fields)

        # Determine the correct step based on what data is still missing
        next_step = _determine_next_step(params)

        # step_date needs available_days pre-computed in params
        if next_step == "step_date":
            free = booking_logic.get_free_windows()
            params["available_days"] = [str(d) for d in sorted({w["date"] for w in free})]

        postgres.upsert_session(
            _BOT_NAME,
            chat_id=chat_id,
            state=next_step,
            params=params,
            object_id=booking_id,
        )
        logger.info(
            "[LLM_BOOKING] Draft booking_id=%d → session=%s (chat=%s)",
            booking_id, next_step, chat_id,
        )
        return booking_id

    # ──────────────────────────────────────────────────────────────────────
    #  Input validation helpers
    #
    #  Each returns a normalized value if valid, or None if the input
    #  is missing / malformed / out of bounds.  The LLM may produce
    #  unexpected formats, so these are intentionally defensive.
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_date(value) -> str | None:
        """
        Parse and validate a date string (expected YYYY-MM-DD).
        Must fall within the 7-day booking window: [today, today + 6 days].
        """
        if not value:
            return None
        try:
            d = datetime.strptime(str(value), "%Y-%m-%d").date()
        except ValueError:
            return None
        today = today_almaty()
        if d < today or d > today + timedelta(days=6):
            return None
        return str(d)

    @staticmethod
    def _validate_time(value) -> str | None:
        """
        Parse and validate a time string (expected HH:MM).
        Must be within the venue's operating hours (config.BOOKING_OPEN/CLOSE_TIME).
        """
        if not value:
            return None
        try:
            t = datetime.strptime(str(value).strip(), "%H:%M").time()
        except ValueError:
            return None
        open_t  = datetime.strptime(config.BOOKING_OPEN_TIME, "%H:%M").time()
        close_t = datetime.strptime(config.BOOKING_CLOSE_TIME, "%H:%M").time()
        if t < open_t or t > close_t:
            return None
        return t.strftime("%H:%M")

    @staticmethod
    def _validate_field(value) -> int | None:
        """Validate that the field ID matches one of the configured fields in config.BOOKING_FIELDS."""
        if value is None:
            return None
        try:
            fid = int(value)
        except (ValueError, TypeError):
            return None
        if any(f["id"] == fid for f in config.BOOKING_FIELDS):
            return fid
        return None

    @staticmethod
    def _validate_players(value) -> int | None:
        """Validate player count — must be a positive integer."""
        if value is None:
            return None
        try:
            p = int(value)
            return p if p > 0 else None
        except (ValueError, TypeError):
            return None


# ─────────────────────────────────────────────────────────────────────────
#  Module-level helpers
# ─────────────────────────────────────────────────────────────────────────

def _fmt_date(d: date) -> str:
    """Format a date as 'Чт 12.06.2026' for user-facing messages."""
    return f"{_WEEKDAY_RU[d.weekday()]} {d.strftime('%d.%m.%Y')}"


def _list_fields() -> str:
    """Comma-separated list of all configured fields, e.g. 'Поле 1 (5x5), Поле 2 (6x6)'."""
    return ", ".join(f"Поле {f['id']} ({f['format']})" for f in config.BOOKING_FIELDS)


def _get_free_fields_for_slot(date_str: str, time_start: str, time_end: str) -> list[dict]:
    """
    Return the subset of config.BOOKING_FIELDS that are free for the given
    date and time range.  Used by both _check_date_and_time and the auto-select
    logic in handle().
    """
    week_start, week_end = booking_logic.get_week_range()
    booked = booking_logic.get_all_booked(week_start, week_end)
    return [
        f for f in config.BOOKING_FIELDS
        if booking_logic.is_range_free(booked, date_str, time_start, time_end, f["id"])
    ]


def _determine_next_step(params: dict) -> str:
    """
    Given collected booking params, determine which step the deterministic
    flow should resume from.

    Step order: date → time → field → players → name → confirm
    We return the earliest step whose required data is still missing.
    """
    if not params.get("date"):
        return "step_date"
    if not params.get("time_start") or not params.get("time_end"):
        return "step_time"
    if not params.get("field"):
        return "step_field"
    if not params.get("players"):
        return "step_players"
    if not params.get("customer_name"):
        return "step_name"
    return "step_confirm"