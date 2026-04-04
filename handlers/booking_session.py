"""
Multi-turn booking session handler (Bot 1 — Dopshy field rental only).

States
------
collecting      LLM extracts params from each message; asks for missing ones.
confirming      Summary shown; waiting for yes / no from the user.

After confirming "yes":
  - Booking written to PostgreSQL (status = awaiting_payment)
  - Row appended to Google Sheets (background thread)
  - Kaspi payment link sent
  - Session deleted
"""

import logging
import threading
from datetime import datetime, timedelta

import config
from chat.llm import extract_booking_params, get_booking_reply
from integrations import booking as booking_logic
from integrations import postgres, sheets

logger = logging.getLogger(__name__)

_WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

_AVAILABILITY_KW = {
    "свободн", "есть место", "есть слот", "занят", "расписани",
    "когда можно", "доступн", "бос", "бар ма", "кесте", "уақыт бар",
}
_MY_BOOKING_KW = {
    "моя бронь", "мой слот", "моё время", "мое время", "я забронировал",
    "мне забронировали", "моя игра", "моя запись",
    "менің брон", "менің уақыт", "менің жазба",
}
_NEW_BOOKING_KW = {
    "забронировать", "забронируй", "хочу поле", "хочу заброн",
    "запишите", "запиши меня", "брондау", "брон жаса", "алаң брон",
    "хочу забронировать", "хочу арендовать",
}


def detect_intent(text: str) -> str | None:
    lower = text.lower()
    for kw in _MY_BOOKING_KW:
        if kw in lower:
            return "my_booking"
    for kw in _NEW_BOOKING_KW:
        if kw in lower:
            return "new_booking"
    for kw in _AVAILABILITY_KW:
        if kw in lower:
            return "availability"
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

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
    intent = detect_intent(user_text)

    # Nothing booking-related — let the normal pipeline handle it
    if not session and not intent:
        return None

    # ── Read-only queries (no session needed) ────────────────────────────
    if not session and intent == "availability":
        free = booking_logic.get_free_slots()
        ctx = booking_logic.format_availability_context(free)
        return get_booking_reply(user_text, ctx)

    if not session and intent == "my_booking":
        bookings = postgres.get_user_upcoming_bookings(sender_phone)
        ctx = booking_logic.format_user_booking_context(bookings)
        return get_booking_reply(user_text, ctx)

    # ── Start a new booking session ──────────────────────────────────────
    if not session and intent == "new_booking":
        free = booking_logic.get_free_slots()
        availability = booking_logic.format_availability_context(free)
        postgres.upsert_session(chat_id, "collecting", {"sender_phone": sender_phone})
        return get_booking_reply(
            user_text,
            availability,
            system_hint=(
                "Пользователь хочет забронировать поле. "
                "Покажи свободные слоты и попроси уточнить: "
                "дату, время начала, формат поля (5x5 или 6x6), "
                "количество игроков и имя клиента."
            ),
        )

    # ── Continue existing session ────────────────────────────────────────
    if session:
        state = session["state"]
        params = session["params"]

        if state == "collecting":
            return _handle_collecting(chat_id, sender_phone, user_text, params)

        if state == "confirming":
            return _handle_confirming(
                chat_id, phone_number_id, sender_phone, user_text, params
            )

    return None


# ---------------------------------------------------------------------------
# State handlers
# ---------------------------------------------------------------------------

def _handle_collecting(
    chat_id: str,
    sender_phone: str,
    user_text: str,
    params: dict,
) -> str:
    free = booking_logic.get_free_slots()
    free_summary = booking_logic.format_availability_context(free)

    extracted = extract_booking_params(user_text, params, free_summary)
    # Merge: only overwrite with non-null extracted values
    merged = {**params, **{k: v for k, v in extracted.items() if v is not None}}
    merged["sender_phone"] = sender_phone

    # Validate slot if date + time_start are known
    if merged.get("date") and merged.get("time_start"):
        format_ = merged.get("format")
        field = merged.get("field")

        # Try to assign a field if not yet set
        if not field and format_:
            field = booking_logic.find_free_field(
                free, merged["date"], merged["time_start"], format_
            )
            if field:
                merged["field"] = field

        # Check if the chosen slot is actually free
        if field and _is_slot_booked(free, merged["date"], merged["time_start"], field):
            postgres.upsert_session(chat_id, "collecting", merged)
            return (
                f"К сожалению, поле {field} в {merged['time_start']} уже занято. "
                f"Пожалуйста, выберите другой слот.\n\n{free_summary}"
            )

    required = ["date", "time_start", "format", "players", "customer_name"]
    missing = [f for f in required if not merged.get(f)]

    if not missing:
        postgres.upsert_session(chat_id, "confirming", merged)
        return _format_summary(merged)

    postgres.upsert_session(chat_id, "collecting", merged)
    return get_booking_reply(
        user_text,
        free_summary,
        system_hint=_missing_hint(missing),
    )


def _handle_confirming(
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
        return _confirm_booking(chat_id, sender_phone, params)

    if any(w in lower for w in _NO):
        # Keep params but go back to collecting so user can adjust
        postgres.upsert_session(chat_id, "collecting", params)
        free = booking_logic.get_free_slots()
        return (
            "Хорошо, давайте изменим детали. "
            "Что именно хотите изменить?\n\n"
            + booking_logic.format_availability_context(free)
        )

    # Ambiguous — re-show summary
    return _format_summary(params) + "\n\nПодтвердить бронь? Ответьте *да* или *нет*."


def _confirm_booking(chat_id: str, sender_phone: str, params: dict) -> str:
    time_start_str = params["time_start"]
    time_end_dt = datetime.strptime(time_start_str, "%H:%M") + timedelta(
        minutes=config.BOOKING_SLOT_DURATION
    )
    time_end_str = time_end_dt.strftime("%H:%M")

    # Resolve field: use collected field, or pick from config by format
    field = params.get("field")
    if not field:
        format_ = params.get("format", "")
        field = next(
            (f["id"] for f in config.BOOKING_FIELDS if f["format"] == format_),
            config.BOOKING_FIELDS[0]["id"],
        )

    try:
        booking_id = postgres.create_booking(
            phone=sender_phone,
            customer_name=params.get("customer_name", ""),
            date=params["date"],
            time_start=time_start_str,
            time_end=time_end_str,
            field=int(field),
            format_=params["format"],
            players=int(params.get("players") or 0),
        )
    except Exception as exc:
        if "unique" in str(exc).lower():
            # Race condition — slot was taken while user was confirming
            postgres.upsert_session(chat_id, "collecting", params)
            free = booking_logic.get_free_slots()
            return (
                "К сожалению, этот слот только что заняли. "
                "Пожалуйста, выберите другое время.\n\n"
                + booking_logic.format_availability_context(free)
            )
        logger.exception("create_booking failed: %s", exc)
        postgres.delete_session(chat_id)
        return (
            "Произошла ошибка при создании брони. Пожалуйста, попробуйте ещё раз.\n"
            "Если проблема повторяется, свяжитесь с администратором."
        )

    # Persist booking details for Sheets write
    booking_data = {
        "id": booking_id,
        "date": params["date"],
        "time_start": time_start_str,
        "time_end": time_end_str,
        "field": field,
        "format": params["format"],
        "players": params.get("players"),
        "customer_name": params.get("customer_name", ""),
        "phone": sender_phone,
        "status": "awaiting_payment",
        "notes": "",
    }

    def _write_to_sheets():
        try:
            sheets.maybe_refresh_week()
            row = sheets.append_booking(booking_data)
            if row:
                postgres.set_booking_sheet_row(booking_id, row)
        except Exception as e:
            logger.error("Sheets write failed for booking %d: %s", booking_id, e)

    threading.Thread(target=_write_to_sheets, daemon=True).start()
    postgres.delete_session(chat_id)

    date_display = _fmt_date(params["date"])
    return (
        f"✅ Бронь подтверждена!\n\n"
        f"📅 {date_display}\n"
        f"⏰ {time_start_str}–{time_end_str}\n"
        f"⚽ Поле {field} ({params['format']})\n"
        f"👥 {params.get('players')} игроков\n"
        f"👤 {params.get('customer_name', '')}\n\n"
        f"Для закрепления брони внесите предоплату:\n"
        f"{config.KASPI_PAYMENT_URL}\n\n"
        f"После оплаты отправьте скриншот чека — и мы подтвердим вашу бронь. 🙏"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_slot_booked(free_slots: list[dict], date_str: str, time_str: str, field: int) -> bool:
    """Return True if the slot is NOT in free_slots (i.e. it is already booked)."""
    for s in free_slots:
        if (
            str(s["date"]) == date_str
            and s["time_start"].strftime("%H:%M") == time_str
            and s["field"] == int(field)
        ):
            return False  # found in free list → not booked
    return True  # not found → taken


def _format_summary(params: dict) -> str:
    time_start = params.get("time_start", "?")
    if time_start != "?":
        try:
            te = (
                datetime.strptime(time_start, "%H:%M")
                + timedelta(minutes=config.BOOKING_SLOT_DURATION)
            ).strftime("%H:%M")
        except ValueError:
            te = "?"
    else:
        te = "?"

    date_display = _fmt_date(params.get("date", "")) or "?"
    field_display = params.get("field", "?")

    return (
        f"📋 Детали брони:\n"
        f"📅 {date_display}\n"
        f"⏰ {time_start}–{te}\n"
        f"⚽ Поле {field_display} ({params.get('format', '?')})\n"
        f"👥 Игроков: {params.get('players', '?')}\n"
        f"👤 Имя: {params.get('customer_name', '?')}\n\n"
        f"Подтвердить? Ответьте *да* или *нет*."
    )


def _fmt_date(date_str: str) -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        return f"{_WEEKDAY_RU[d.weekday()]} {d.strftime('%d.%m.%Y')}"
    except (ValueError, TypeError):
        return date_str or ""


_FIELD_NAMES_RU = {
    "date":          "дату",
    "time_start":    "время начала",
    "format":        "формат поля (5x5 или 6x6)",
    "players":       "количество игроков",
    "customer_name": "ваше имя",
}


def _missing_hint(missing: list[str]) -> str:
    names = [_FIELD_NAMES_RU.get(f, f) for f in missing]
    return f"Уточни ещё: {', '.join(names)}."
