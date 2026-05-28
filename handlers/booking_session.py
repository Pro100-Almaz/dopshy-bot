"""
Deterministic step-by-step booking session handler (Bot 1 — Dopshy field rental only).

States
------
step_date       Show numbered list of available days; user picks one.
step_time       Show free windows for chosen day; user enters "HH:MM до HH:MM".
step_field      If multiple fields free for that time: user picks one. (Auto-skipped if only one.)
step_players    Ask player count; user enters an integer.
step_name       Ask customer name; user enters free text.
step_confirm    Show summary; user replies да / нет.

After confirming "да":
  - Booking written to PostgreSQL (status = awaiting_payment)
  - Google Sheets refreshed (background thread)
  - Kaspi payment link sent
  - Session deleted
"""

import logging
import re
import threading
import uuid
from datetime import date, datetime

import config
from integrations import booking as booking_logic
from integrations import booking_service, postgres, sheets
from utils import today_almaty

logger = logging.getLogger(__name__)

_WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _save(chat_id: str, state: str, params: dict) -> None:
    """Persist the session, keeping booking_sessions.booking_id in sync."""
    postgres.upsert_session(chat_id, state, params, booking_id=params.get("booking_id"))

# Regex to pull two HH:MM times from a single message (e.g. "10:00 до 12:00", "14:30-16:00")
_TIME_RANGE_RE = re.compile(r"(\d{1,2}:\d{2})\s*[-–—до\s]+\s*(\d{1,2}:\d{2})")

# ---------------------------------------------------------------------------
# Read-only intent keywords (keep for quick queries without starting a session)
# ---------------------------------------------------------------------------

_MY_BOOKING_KW = {
    "моя бронь", "мой слот", "моё время", "мое время", "я забронировал",
    "мне забронировали", "моя игра", "моя запись",
    "менің брон", "менің уақыт", "менің жазба",
}

# Substrings (matched in lowercased message) that abort an in-flight booking
# session at any step. Strong cancel intents only — short tokens like "нет" are
# intentionally excluded because they're valid step_confirm inputs.
_CANCEL_PHRASES = (
    "отмен", "стоп", "передум", "не хочу", "не хотим", "не нужно",
    "не надо", "забуд", "забыть", "отбой", "отказыв",
    "тоқтат", "керек емес", "ұнамайды", "болмайды", "бас тарт",
)


def _is_cancel_intent(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in _CANCEL_PHRASES)


def detect_intent(text: str) -> str | None:
    """
    Detects only my_booking intent — requires injecting user-specific data
    the LLM cannot fetch on its own.

    new_booking: handled by LLM via [BOOK] tag.
    availability: handled by LLM with live slot data injected in message_handler.py.
    """
    lower = text.lower()
    for kw in _MY_BOOKING_KW:
        if kw in lower:
            return "my_booking"
    return None


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def start_booking_flow(chat_id: str, sender_phone: str) -> str:
    """
    Create a new booking session (step_date) and return the date-selection prompt.
    Called from message_handler when LLM reply contains [BOOK].
    """
    free = booking_logic.get_free_windows()
    available_days = sorted({w["date"] for w in free})
    logger.info(
        "[BOOKING:start_flow] chat_id=%s free_windows=%d available_days=%s",
        chat_id, len(free), available_days,
    )

    if not available_days:
        logger.warning("[BOOKING:start_flow] No available days — aborting flow")
        return "К сожалению, свободных слотов на ближайшие 7 дней нет. Пожалуйста, свяжитесь с администратором."

    client_token = str(uuid.uuid4())
    draft = booking_service.create_draft(chat_id, sender_phone, client_token)
    booking_id = draft["data"]["booking_id"]

    _save(
        chat_id,
        "step_date",
        {
            "sender_phone": sender_phone,
            "available_days": [str(d) for d in available_days],
            "booking_id": booking_id,
            "client_token": client_token,
        },
    )
    logger.info(
        "[BOOKING:start_flow] Draft booking_id=%d created — step_date. Showing %d days",
        booking_id, len(available_days),
    )
    return _ask_date(available_days)


def handle_booking_turn(
    chat_id: str,
    phone_number_id: str,
    sender_phone: str,
    user_text: str,
) -> str | None:
    """
    Handle one message turn for booking-related flows.

    Returns a reply string if this turn was handled, or None to fall
    through to the regular RAG/LLM pipeline.
    """
    session = postgres.get_active_session(chat_id)

    # ── Active session — dispatch to step handler ────────────────────────
    if session:
        state  = session["state"]
        params = session["params"]
        logger.info(
            "[BOOKING] Active session found: chat_id=%s state=%s params=%s | user_text=%.80s",
            chat_id, state, params, user_text,
        )

        # Natural-language cancel at any step ("передумал", "отмена", "не хотим"…)
        if _is_cancel_intent(user_text):
            bid = params.get("booking_id")
            if bid:
                booking_service.cancel_booking(
                    bid, actor_type="whatsapp", actor_id=chat_id, reason="user_cancel_mid_flow"
                )
            else:
                postgres.delete_session(chat_id)
            logger.info("[BOOKING] Cancel intent detected — session cleared for %s", chat_id)
            return ("Хорошо, бронь отменена. Если передумаете — просто напишите! 🙂\n\n"
                    "Жарайды, брондау тоқтатылды. Қайта қаласаңыз — жазыңыз!")

        # Legacy session from before the state-machine upgrade has no draft booking_id.
        # Discard it and restart the flow cleanly rather than crashing.
        if "booking_id" not in params:
            logger.warning("[BOOKING] Stale session without booking_id — restarting flow for %s", chat_id)
            postgres.delete_session(chat_id)
            return start_booking_flow(chat_id, sender_phone)

        if state == "step_date":
            return _handle_step_date(chat_id, user_text, params)
        if state == "step_time":
            return _handle_step_time(chat_id, user_text, params)
        if state == "step_field":
            return _handle_step_field(chat_id, user_text, params)
        if state == "step_players":
            return _handle_step_players(chat_id, user_text, params)
        if state == "step_name":
            return _handle_step_name(chat_id, user_text, params)
        if state == "step_confirm":
            return _handle_step_confirm(chat_id, phone_number_id, sender_phone, user_text, params)
        logger.warning("[BOOKING] Unknown session state=%s — falling through", state)
        return None

    # ── No active session — check intents ───────────────────────────────
    # new_booking intent is handled by the LLM via [BOOK] tag (see message_handler.py).
    # Only intercept my_booking here — it requires injecting user-specific data
    # that the LLM cannot fetch on its own.
    from chat.llm import get_booking_reply

    intent = detect_intent(user_text)
    logger.info("[BOOKING] No active session. intent=%s | user_text=%.80s", intent, user_text)

    if intent == "my_booking":
        bookings = postgres.get_user_upcoming_bookings(sender_phone)
        logger.info("[BOOKING] my_booking query — %d bookings found for %s", len(bookings), sender_phone)
        ctx = booking_logic.format_user_booking_context(bookings)
        return get_booking_reply(user_text, ctx)

    # All other intents (including new_booking and availability) fall through
    # to the RAG/LLM pipeline. The LLM emits [BOOK] to start a booking flow,
    # and receives live availability data injected by message_handler.py.
    return None


# ---------------------------------------------------------------------------
# Step handlers
# ---------------------------------------------------------------------------

def _handle_step_date(chat_id: str, user_text: str, params: dict) -> str:
    # Always recompute available_days here so a session that crossed midnight
    # doesn't keep offering yesterday's date.
    free_now = booking_logic.get_free_windows()
    available_days = sorted({w["date"] for w in free_now})
    params["available_days"] = [str(d) for d in available_days]
    logger.info("[BOOKING:step_date] available_days=%s | user_text=%.80s", available_days, user_text)

    chosen: date | None = None
    text = user_text.strip()

    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(available_days):
            chosen = available_days[idx]
        logger.info("[BOOKING:step_date] numeric input=%s idx=%d chosen=%s", text, idx, chosen)
    else:
        for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                chosen = datetime.strptime(text, fmt).date()
                logger.info("[BOOKING:step_date] parsed date via fmt=%s → %s", fmt, chosen)
                break
            except ValueError:
                pass
        # Also allow "dd.mm" without year
        if not chosen:
            m = re.match(r"^(\d{1,2})\.(\d{1,2})$", text)
            if m:
                try:
                    chosen = date(today_almaty().year, int(m.group(2)), int(m.group(1)))
                    logger.info("[BOOKING:step_date] parsed dd.mm → %s", chosen)
                except ValueError:
                    pass

    if not chosen or chosen not in available_days:
        logger.info(
            "[BOOKING:step_date] REJECTED — chosen=%s not in available_days=%s",
            chosen, available_days,
        )
        return _ask_date(available_days) + "\n\nПожалуйста, введите номер из списка."

    free = booking_logic.get_free_windows()
    day_windows = [w for w in free if w["date"] == chosen]
    logger.info(
        "[BOOKING:step_date] ACCEPTED date=%s — %d free windows → advancing to step_time",
        chosen, len(day_windows),
    )

    params["date"] = str(chosen)
    booking_service.update_draft(params["booking_id"], date=str(chosen))
    _save(chat_id, "step_time", params)
    return _ask_time(chosen, day_windows)


def _handle_step_time(chat_id: str, user_text: str, params: dict) -> str:
    chosen_date = datetime.strptime(params["date"], "%Y-%m-%d").date()
    free        = booking_logic.get_free_windows()
    day_windows = [w for w in free if w["date"] == chosen_date]
    logger.info(
        "[BOOKING:step_time] date=%s free_windows=%s | user_text=%.80s",
        chosen_date,
        [(w["field"], str(w["time_start"]), str(w["time_end"])) for w in day_windows],
        user_text,
    )

    m = _TIME_RANGE_RE.search(user_text)
    if not m:
        logger.info("[BOOKING:step_time] REJECTED — regex did not match user_text=%.80s", user_text)
        return _ask_time(chosen_date, day_windows) + "\n\nНе распознал время. Пример: *10:00 до 12:00*"

    time_start, time_end = m.group(1), m.group(2)
    time_start = _pad_time(time_start)
    time_end   = _pad_time(time_end)
    logger.info("[BOOKING:step_time] parsed time_start=%s time_end=%s", time_start, time_end)

    week_start, week_end = booking_logic.get_week_range()
    booked = booking_logic.get_all_booked(week_start, week_end)
    logger.info(
        "[BOOKING:step_time] booked slots for week %s–%s: %d total",
        week_start, week_end, len(booked),
    )

    free_fields = [
        f for f in config.BOOKING_FIELDS
        if booking_logic.is_range_free(booked, params["date"], time_start, time_end, f["id"])
    ]
    logger.info(
        "[BOOKING:step_time] free_fields for %s %s–%s: %s",
        params["date"], time_start, time_end,
        [f["id"] for f in free_fields],
    )

    if not free_fields:
        logger.info("[BOOKING:step_time] REJECTED — no free fields for requested time")
        return (
            f"К сожалению, нет свободных полей с {time_start} до {time_end}. "
            f"Выберите другое время.\n\n"
            + _ask_time(chosen_date, day_windows)
        )

    params["time_start"] = time_start
    params["time_end"]   = time_end

    if len(free_fields) == 1:
        f = free_fields[0]
        params["field"]  = f["id"]
        params["format"] = f["format"]
        logger.info("[BOOKING:step_time] single free field=%d — advancing to step_players", f["id"])
        booking_service.update_draft(
            params["booking_id"], time_start=time_start, time_end=time_end,
            field=f["id"], format=f["format"],
        )
        _save(chat_id, "step_players", params)
        return f"Поле {f['id']} ({f['format']}) — свободно ✅\n\nСколько игроков будет?"

    logger.info("[BOOKING:step_time] multiple free fields=%s — advancing to step_field", [f["id"] for f in free_fields])
    booking_service.update_draft(params["booking_id"], time_start=time_start, time_end=time_end)
    _save(chat_id, "step_field", params)
    return _ask_field(free_fields)


def _handle_step_field(chat_id: str, user_text: str, params: dict) -> str:
    week_start, week_end = booking_logic.get_week_range()
    booked      = booking_logic.get_all_booked(week_start, week_end)
    free_fields = [
        f for f in config.BOOKING_FIELDS
        if booking_logic.is_range_free(booked, params["date"], params["time_start"], params["time_end"], f["id"])
    ]

    chosen_field = None
    m = re.search(r"\b(\d+)\b", user_text)
    if m:
        num = int(m.group(1))
        # Match by field id first, then by 1-based list position
        chosen_field = (
            next((f for f in free_fields if f["id"] == num), None)
            or (free_fields[num - 1] if 1 <= num <= len(free_fields) else None)
        )

    if not chosen_field:
        return _ask_field(free_fields) + "\n\nПожалуйста, введите номер поля."

    params["field"]  = chosen_field["id"]
    params["format"] = chosen_field["format"]
    booking_service.update_draft(
        params["booking_id"], field=chosen_field["id"], format=chosen_field["format"]
    )
    _save(chat_id, "step_players", params)
    return "Сколько игроков будет?"


def _handle_step_players(chat_id: str, user_text: str, params: dict) -> str:
    m = re.search(r"\b(\d+)\b", user_text)
    if not m:
        logger.info("[BOOKING:step_players] REJECTED — no digit found in user_text=%.80s", user_text)
        return "Пожалуйста, введите количество игроков (например: *8*)."

    params["players"] = int(m.group(1))
    logger.info("[BOOKING:step_players] players=%d — advancing to step_name", params["players"])
    booking_service.update_draft(params["booking_id"], players=params["players"])
    _save(chat_id, "step_name", params)
    return "Укажите ваше имя:"


def _handle_step_name(chat_id: str, user_text: str, params: dict) -> str:
    params["customer_name"] = user_text.strip()
    logger.info("[BOOKING:step_name] customer_name=%r — advancing to step_confirm", params["customer_name"])
    booking_service.update_draft(params["booking_id"], customer_name=params["customer_name"])
    _save(chat_id, "step_confirm", params)
    return _format_summary(params)


def _handle_step_confirm(
    chat_id: str,
    phone_number_id: str,
    sender_phone: str,
    user_text: str,
    params: dict,
) -> str:
    lower = user_text.lower().strip()

    _YES = {"да", "иә", "ok", "ок", "подтверждаю", "yes", "жарайды", "дұрыс", "растаймын", "👍"}
    _NO  = {"нет", "жоқ", "no", "отмена", "изменить", "өзгерт", "болмайды", "бастапқы"}

    if any(w in lower for w in _YES):
        logger.info("[BOOKING:step_confirm] YES received — confirming booking. params=%s", params)
        return _confirm_booking(chat_id, sender_phone, params)

    if any(w in lower for w in _NO):
        logger.info("[BOOKING:step_confirm] NO received — cancelling draft + session")
        if params.get("booking_id"):
            booking_service.cancel_booking(
                params["booking_id"], actor_type="whatsapp", actor_id=chat_id, reason="user_declined"
            )
        postgres.delete_session(chat_id)
        return (
            "Бронирование отменено. Если захотите снова — просто напишите, "
            "что хотите забронировать поле. 🙂"
        )

    logger.info("[BOOKING:step_confirm] unrecognised response=%.80s — re-showing summary", user_text)
    return _format_summary(params) + "\n\nПодтвердить бронь? Ответьте *да* или *нет*."


# ---------------------------------------------------------------------------
# Booking confirmation
# ---------------------------------------------------------------------------

def _confirm_booking(chat_id: str, sender_phone: str, params: dict) -> str:
    time_start_str = params["time_start"]
    time_end_str   = params["time_end"]
    field          = int(params["field"])
    booking_id     = params["booking_id"]

    logger.info(
        "[BOOKING:confirm] request_payment booking_id=%d phone=%s date=%s %s–%s field=%d",
        booking_id, sender_phone, params["date"], time_start_str, time_end_str, field,
    )

    res = booking_service.request_payment(booking_id, params["client_token"])
    if not res["ok"]:
        postgres.delete_session(chat_id)
        if res["code"] == "SLOT_TAKEN":
            logger.warning("[BOOKING:confirm] slot taken mid-flow for booking_id=%d", booking_id)
            free = booking_logic.get_free_windows()
            return (
                "К сожалению, этот слот только что заняли. "
                "Начните бронирование заново.\n\n"
                + booking_logic.format_availability_context(free)
            )
        logger.error("[BOOKING:confirm] request_payment failed: %s", res)
        return (
            "Произошла ошибка при создании брони. Пожалуйста, попробуйте ещё раз.\n"
            "Если проблема повторяется, свяжитесь с администратором."
        )

    logger.info("[BOOKING:confirm] booking_id=%d → awaiting_payment", booking_id)

    booking_row = {
        "id":            booking_id,
        "field":         field,
        "date":          params["date"],
        "time_start":    time_start_str,
        "time_end":      time_end_str,
        "customer_name": params.get("customer_name", ""),
        "phone":         sender_phone,
        "players":       params.get("players"),
        "state":         "awaiting_payment",
        "notes":         "",
    }

    def _write_to_sheets():
        try:
            sheets.upsert_booking_row(booking_row)
        except Exception as e:
            logger.error("Sheets write failed for booking %d: %s", booking_id, e)

    threading.Thread(target=_write_to_sheets, daemon=True).start()
    postgres.delete_session(chat_id)

    date_display = _fmt_date(params["date"])
    return (
        f"📋 Бронь зарегистрирована, но ещё не подтверждена!\n\n"
        f"📅 {date_display}\n"
        f"⏰ {time_start_str}–{time_end_str}\n"
        f"⚽ Поле {field} ({params['format']})\n"
        f"👥 {params.get('players')} игроков\n"
        f"👤 {params.get('customer_name', '')}\n\n"
        f"⏳ Статус: ожидает оплаты\n\n"
        f"Для подтверждения брони оплатите по ссылке:\n"
        f"{config.KASPI_PAYMENT_URL}\n\n"
        f"После оплаты отправьте PDF-чек из Kaspi сюда в чат — "
        f"и мы сразу подтвердим вашу бронь. 🙏\n\n"
        f"⚠️ Если оплата не поступит в течение 1 часа — бронь будет автоматически отменена.\n\n"
        f"— — —\n"
        f"📋 Брондау тіркелді, бірақ әлі расталмады!\n\n"
        f"⏳ Күй: төлем күтілуде\n\n"
        f"Брондауды растау үшін төлем жасаңыз:\n"
        f"{config.KASPI_PAYMENT_URL}\n\n"
        f"Төлегеннен кейін Kaspi-дің PDF-чекін осы чатқа жіберіңіз — "
        f"брондауыңызды бірден растаймыз. 🙏\n\n"
        f"⚠️ 1 сағат ішінде төлем келмесе — бронь автоматты түрде жойылады."
    )


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _ask_date(available_days: list[date]) -> str:
    lines = ["📅 Выберите дату (введите номер):"]
    for i, d in enumerate(available_days, 1):
        lines.append(f"  {i}. {_WEEKDAY_RU[d.weekday()]} {d.strftime('%d.%m.%Y')}")
    return "\n".join(lines)


def _ask_time(chosen_date: date, day_windows: list[dict]) -> str:
    lines = [f"📅 {_WEEKDAY_RU[chosen_date.weekday()]} {chosen_date.strftime('%d.%m.%Y')}\n"]
    lines.append("Свободное время по полям:")

    by_field: dict = {}
    for w in day_windows:
        by_field.setdefault((w["field"], w["format"]), []).append(w)

    for (field_id, fmt) in sorted(by_field):
        windows = sorted(by_field[(field_id, fmt)], key=lambda w: w["time_start"])
        range_str = ", ".join(
            f"{w['time_start'].strftime('%H:%M')}–{w['time_end'].strftime('%H:%M')}"
            for w in windows
        )
        lines.append(f"  Поле {field_id} ({fmt}): {range_str}")

    lines.append("\nВведите время начала и окончания:")
    lines.append("Например: *10:00 до 12:00* или *10:20-11:45*")
    return "\n".join(lines)


def _ask_field(free_fields: list[dict]) -> str:
    lines = ["Выберите поле:"]
    for f in free_fields:
        lines.append(f"  {f['id']}. Поле {f['id']} ({f['format']})")
    return "\n".join(lines)


def _format_summary(params: dict) -> str:
    return (
        f"📋 Детали брони:\n"
        f"📅 {_fmt_date(params.get('date', ''))}\n"
        f"⏰ {params.get('time_start', '?')}–{params.get('time_end', '?')}\n"
        f"⚽ Поле {params.get('field', '?')} ({params.get('format', '?')})\n"
        f"👥 Игроков: {params.get('players', '?')}\n"
        f"👤 Имя: {params.get('customer_name', '?')}\n\n"
        f"Подтвердить? Ответьте *да* или *нет*."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_date(date_str: str) -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        return f"{_WEEKDAY_RU[d.weekday()]} {d.strftime('%d.%m.%Y')}"
    except (ValueError, TypeError):
        return date_str or "?"


def _pad_time(t: str) -> str:
    """Ensure HH:MM format (zero-pad single-digit hours)."""
    h, m = t.split(":")
    return f"{int(h):02d}:{m}"


