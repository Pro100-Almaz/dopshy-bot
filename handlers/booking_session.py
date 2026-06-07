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
from integrations import booking_service, sheets
from integrations.repo import booking_repo, postgres
from integrations.sheets.booking_sheets import upsert_booking_row
from utils import today_almaty

logger = logging.getLogger(__name__)

_WEEKDAY = {
    "ru": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
    "kk": ["Дс", "Сс", "Ср", "Бс", "Жм", "Сб", "Жс"],
}
# Cyrillic letters that exist only in Kazakh — presence flips the session lang to kk.
_KZ_CHARS = set("әғіңөұүһқ")

_BOT_NAME = config.BOT_CONFIGS[config.WHATSAPP_PHONE_NUMBER_ID_BOT_1]['name']

def _detect_lang(text: str) -> str:
    """Crude lang detection: any Kazakh-only Cyrillic letter → kk, else ru."""
    return "kk" if any(c in _KZ_CHARS for c in text.lower()) else "ru"


_T = {
    "ask_date_header":        {"ru": "📅 Выберите дату (введите номер):",
                                "kk": "📅 Күнді таңдаңыз (нөмірді енгізіңіз):"},
    "ask_date_invalid":       {"ru": "Пожалуйста, введите номер из списка.",
                                "kk": "Тізімдегі нөмірді енгізіңіз."},
    "ask_time_header":        {"ru": "Свободное время по полям:",
                                "kk": "Алаңдардың бос уақыты:"},
    "ask_time_prompt":        {"ru": "Введите время начала и окончания:",
                                "kk": "Басталу және аяқталу уақытын енгізіңіз:"},
    "ask_time_example":       {"ru": "Например: *10:00 до 12:00* или *10:20-11:45*",
                                "kk": "Мысалы: *10:00 - 12:00* немесе *10:20-11:45*"},
    "time_not_recognized":    {"ru": "Не распознал время. Пример: *10:00 до 12:00*",
                                "kk": "Уақыт танылмады. Мысалы: *10:00 - 12:00*"},
    "time_inverted":          {"ru": "Время окончания должно быть позже времени начала. Пример: *10:00 до 12:00*",
                                "kk": "Аяқталу уақыты басталу уақытынан кейін болуы керек. Мысалы: *10:00 - 12:00*"},
    "no_free_fields":         {"ru": "К сожалению, нет свободных полей с {start} до {end}. Выберите другое время.",
                                "kk": "Өкінішке орай, {start}–{end} аралығында бос алаң жоқ. Басқа уақыт таңдаңыз."},
    "ask_field_header":       {"ru": "Выберите поле:",
                                "kk": "Алаңды таңдаңыз:"},
    "ask_field_invalid":      {"ru": "Пожалуйста, введите номер поля.",
                                "kk": "Алаң нөмірін енгізіңіз."},
    "field_free_advance":     {"ru": "Поле {id} ({fmt}) — свободно ✅\n\nСколько игроков будет?",
                                "kk": "Алаң {id} ({fmt}) — бос ✅\n\nҚанша ойыншы болады?"},
    "ask_players":            {"ru": "Сколько игроков будет?",
                                "kk": "Қанша ойыншы болады?"},
    "ask_players_invalid":    {"ru": "Пожалуйста, введите количество игроков (например: *8*).",
                                "kk": "Ойыншылар санын енгізіңіз (мысалы: *8*)."},
    "ask_name":               {"ru": "Укажите ваше имя:",
                                "kk": "Атыңызды жазыңыз:"},
    "summary":                {"ru": "📋 Детали брони:\n📅 {date}\n⏰ {start}–{end}\n⚽ Поле {field} ({fmt})\n👥 Игроков: {players}\n👤 Имя: {name}\n\nПодтвердить? Ответьте *да* или *нет*.",
                                "kk": "📋 Брондау деректері:\n📅 {date}\n⏰ {start}–{end}\n⚽ Алаң {field} ({fmt})\n👥 Ойыншылар: {players}\n👤 Аты: {name}\n\nРастайсыз ба? *иә* немесе *жоқ* деп жауап беріңіз."},
    "confirm_reshow":         {"ru": "Подтвердить бронь? Ответьте *да* или *нет*.",
                                "kk": "Брондауды растайсыз ба? *иә* немесе *жоқ* деп жауап беріңіз."},
    "declined":               {"ru": "Бронирование отменено. Если захотите снова — просто напишите, что хотите забронировать поле. 🙂",
                                "kk": "Брондау тоқтатылды. Қайта қаласаңыз — алаңды брондағыңыз келетінін жазыңыз. 🙂"},
    "slot_taken":             {"ru": "К сожалению, этот слот только что заняли. Начните бронирование заново.\n\n",
                                "kk": "Өкінішке орай, бұл слотты жаңа ғана алып қойды. Брондауды қайта бастаңыз.\n\n"},
    "request_payment_error":  {"ru": "Произошла ошибка при создании брони. Пожалуйста, попробуйте ещё раз.\nЕсли проблема повторяется, свяжитесь с администратором.",
                                "kk": "Брондау жасау кезінде қате шықты. Қайталап көріңіз.\nҚайталанса — әкімшімен хабарласыңыз."},
    "no_availability":        {"ru": "К сожалению, свободных слотов на ближайшие 7 дней нет. Пожалуйста, свяжитесь с администратором.",
                                "kk": "Өкінішке орай, келесі 7 күнде бос слот жоқ. Әкімшімен хабарласыңыз."},
    "booking_pending":        {"ru": "📋 Бронь зарегистрирована, но ещё не подтверждена!\n\n📅 {date}\n⏰ {start}–{end}\n⚽ Поле {field} ({fmt})\n👥 {players} игроков\n👤 {name}\n\n⏳ Статус: ожидает оплаты\n\nДля подтверждения брони оплатите по ссылке:\n{pay_url}\n\nПосле оплаты отправьте PDF-чек из Kaspi сюда в чат — и мы сразу подтвердим вашу бронь. 🙏\n\n⚠️ Если оплата не поступит в течение 1 часа — бронь будет автоматически отменена.",
                                "kk": "📋 Брондау тіркелді, бірақ әлі расталмады!\n\n📅 {date}\n⏰ {start}–{end}\n⚽ Алаң {field} ({fmt})\n👥 {players} ойыншы\n👤 {name}\n\n⏳ Күй: төлем күтілуде\n\nБрондауды растау үшін төлем жасаңыз:\n{pay_url}\n\nТөлегеннен кейін Kaspi-дің PDF-чекін осы чатқа жіберіңіз — брондауыңызды бірден растаймыз. 🙏\n\n⚠️ 1 сағат ішінде төлем келмесе — бронь автоматты түрде жойылады."},
    "field_label":            {"ru": "Поле", "kk": "Алаң"},
}


def _t(lang: str, key: str, **fmt) -> str:
    val = _T[key].get(lang) or _T[key]["ru"]
    return val.format(**fmt) if fmt else val


def _save(chat_id: str, state: str, params: dict) -> None:
    """Persist the session, keeping booking_sessions.booking_id in sync."""
    postgres.upsert_session(_BOT_NAME, chat_id, state, params, object_id=params.get("booking_id"))

# Regex to pull two HH:MM times from a single message (e.g. "10:00 до 12:00", "14:30-16:00")
_TIME_RANGE_RE = re.compile(r"(\d{1,2}:\d{2})\s*[-–—до\s]+\s*(\d{1,2}:\d{2})")

# ---------------------------------------------------------------------------
# Read-only intent keywords (keep for quick queries without starting a session)
# ---------------------------------------------------------------------------

# Substring stems for "show me my existing booking". Stems are intentionally
# short so Russian declensions (моя/мою/моей/моих) and Kazakh possessives
# (брондауым/брондауыма) are all caught with a single `in` check.
# Checked BEFORE _NEW_BOOKING_KW so past-tense "я забронировал" doesn't get
# misrouted by the "забронир" stem in new_booking.
_MY_BOOKING_KW = (
    # Russian — possessive + key noun (covers multiple declensions)
    "моя брон", "мою брон", "моей брон", "моих брон", "мои брон", "мой брон",
    "мой слот", "моего слот", "моих слот",
    "моё врем", "мое врем", "моего врем",
    "моя игр", "мою игр", "моей игр",
    "моя зап", "моей зап", "моих зап",
    "мой заказ", "моего заказ",
    # Past-tense "I already booked" / "they booked for me" forms
    "я забронир", "мы забронир", "мне забронир", "нам забронир",
    # Verb-led queries about an existing booking
    "покажи брон", "посмотреть брон", "проверить брон", "статус брон",
    "где брон", "когда брон", "что с брон",
    # Kazakh
    "менің брон", "менің уақыт", "менің жазб",
    "брондауым", "брондауыма", "брондауымды", "брондауымның",
)

# Substrings (lowercased) that mean "user wants to make a new booking right now".
# Checked AFTER _MY_BOOKING_KW so phrases like "я забронировал" stay on my_booking.
_NEW_BOOKING_KW = (
    "забронир", "хочу забронир", "хочу поле", "нужно поле", "снять поле",
    "арендова", "хочу играть",
    "брондау", "брон қой", "бронь қой", "қояйын", "брондамақ",
    "алаң жалда",
)

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
    Deterministic intent detection. my_booking is checked first so phrases like
    "я забронировал" don't get routed to new_booking by the "забронир" substring.

    availability: still handled by the LLM with injected slot data.
    """
    lower = text.lower()
    for kw in _MY_BOOKING_KW:
        if kw in lower:
            return "my_booking"
    for kw in _NEW_BOOKING_KW:
        if kw in lower:
            return "new_booking"
    return None


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def start_booking_flow(chat_id: str, sender_phone: str, lang: str = "ru") -> str:
    """
    Create a new booking session (step_date) and return the date-selection prompt.
    Called from message_handler when the LLM calls the start_booking tool, or
    directly from handle_booking_turn on a deterministic booking-intent match.
    `lang` is stored in the session so every subsequent step reuses it.
    """
    free = booking_logic.get_free_windows()
    available_days = sorted({w["date"] for w in free})
    logger.info(
        "[BOOKING:start_flow] chat_id=%s lang=%s free_windows=%d available_days=%s",
        chat_id, lang, len(free), available_days,
    )

    if not available_days:
        logger.warning("[BOOKING:start_flow] No available days — aborting flow")
        return _t(lang, "no_availability")

    client_token = str(uuid.uuid4())
    draft = postgres.create_draft(bot_name=_BOT_NAME, chat_id=chat_id, phone=sender_phone, client_token=client_token)
    booking_id = draft["data"]["booking_id"]

    _save(
        chat_id,
        "step_date",
        {
            "sender_phone": sender_phone,
            "available_days": [str(d) for d in available_days],
            "booking_id": booking_id,
            "client_token": client_token,
            "lang": lang,
        },
    )
    logger.info(
        "[BOOKING:start_flow] Draft booking_id=%d created — step_date. Showing %d days",
        booking_id, len(available_days),
    )
    return _ask_date(available_days, lang)


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
    session = postgres.get_active_session(_BOT_NAME, chat_id)

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
                postgres.cancel_booking_trial(
                    _BOT_NAME, bid, actor_type="whatsapp", actor_id=chat_id, reason="user_cancel_mid_flow"
                )
            else:
                postgres.delete_session(_BOT_NAME, chat_id)
            logger.info("[BOOKING] Cancel intent detected — session cleared for %s", chat_id)
            return ("Хорошо, бронь отменена. Если передумаете — просто напишите! 🙂\n\n"
                    "Жарайды, брондау тоқтатылды. Қайта қаласаңыз — жазыңыз!")

        # Legacy session from before the state-machine upgrade has no draft booking_id.
        # Discard it and restart the flow cleanly rather than crashing.
        if "booking_id" not in params:
            logger.warning("[BOOKING] Stale session without booking_id — restarting flow for %s", chat_id)
            postgres.delete_session(_BOT_NAME, chat_id)
            return start_booking_flow(chat_id, sender_phone, _detect_lang(user_text))

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
        bookings = booking_repo.get_user_upcoming_bookings(sender_phone)
        logger.info("[BOOKING] my_booking query — %d bookings found for %s", len(bookings), sender_phone)
        ctx = booking_logic.format_user_booking_context(bookings)
        return get_booking_reply(user_text, ctx)

    if intent == "new_booking":
        lang = _detect_lang(user_text)
        logger.info("[BOOKING] new_booking intent — starting deterministic flow (lang=%s)", lang)
        return start_booking_flow(chat_id, sender_phone, lang)

    # availability and other intents fall through to the RAG/LLM pipeline.
    # The LLM may still call the start_booking tool for phrasings the keyword
    # list doesn't catch.
    return None


# ---------------------------------------------------------------------------
# Step handlers
# ---------------------------------------------------------------------------

def _handle_step_date(chat_id: str, user_text: str, params: dict) -> str:
    lang = params.get("lang", "ru")
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
        return _ask_date(available_days, lang) + "\n\n" + _t(lang, "ask_date_invalid")

    free = booking_logic.get_free_windows()
    day_windows = [w for w in free if w["date"] == chosen]
    logger.info(
        "[BOOKING:step_date] ACCEPTED date=%s — %d free windows → advancing to step_time",
        chosen, len(day_windows),
    )

    params["date"] = str(chosen)
    postgres.update_draft(_BOT_NAME, params["booking_id"], date=str(chosen))
    _save(chat_id, "step_time", params)
    return _ask_time(chosen, day_windows, lang)


def _handle_step_time(chat_id: str, user_text: str, params: dict) -> str:
    lang = params.get("lang", "ru")
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
        return _ask_time(chosen_date, day_windows, lang) + "\n\n" + _t(lang, "time_not_recognized")

    time_start, time_end = m.group(1), m.group(2)
    time_start = _pad_time(time_start)
    time_end   = _pad_time(time_end)
    logger.info("[BOOKING:step_time] parsed time_start=%s time_end=%s", time_start, time_end)

    if time_start >= time_end:
        logger.info("[BOOKING:step_time] REJECTED — inverted range %s >= %s", time_start, time_end)
        return _ask_time(chosen_date, day_windows, lang) + "\n\n" + _t(lang, "time_inverted")

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
            _t(lang, "no_free_fields", start=time_start, end=time_end)
            + "\n\n"
            + _ask_time(chosen_date, day_windows, lang)
        )

    params["time_start"] = time_start
    params["time_end"]   = time_end

    if len(free_fields) == 1:
        f = free_fields[0]
        params["field"]  = f["id"]
        params["format"] = f["format"]
        logger.info("[BOOKING:step_time] single free field=%d — advancing to step_players", f["id"])
        postgres.update_draft(
            _BOT_NAME, params["booking_id"], time_start=time_start, time_end=time_end,
            field=f["id"], format=f["format"],
        )
        _save(chat_id, "step_players", params)
        return _t(lang, "field_free_advance", id=f["id"], fmt=f["format"])

    logger.info("[BOOKING:step_time] multiple free fields=%s — advancing to step_field", [f["id"] for f in free_fields])
    postgres.update_draft(_BOT_NAME, params["booking_id"], time_start=time_start, time_end=time_end)
    _save(chat_id, "step_field", params)
    return _ask_field(free_fields, lang)


def _handle_step_field(chat_id: str, user_text: str, params: dict) -> str:
    lang = params.get("lang", "ru")
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
        return _ask_field(free_fields, lang) + "\n\n" + _t(lang, "ask_field_invalid")

    params["field"]  = chosen_field["id"]
    params["format"] = chosen_field["format"]
    postgres.update_draft(
        _BOT_NAME, params["booking_id"], field=chosen_field["id"], format=chosen_field["format"]
    )
    _save(chat_id, "step_players", params)
    return _t(lang, "ask_players")


def _handle_step_players(chat_id: str, user_text: str, params: dict) -> str:
    lang = params.get("lang", "ru")
    m = re.search(r"\b(\d+)\b", user_text)
    if not m:
        logger.info("[BOOKING:step_players] REJECTED — no digit found in user_text=%.80s", user_text)
        return _t(lang, "ask_players_invalid")

    params["players"] = int(m.group(1))
    logger.info("[BOOKING:step_players] players=%d — advancing to step_name", params["players"])
    postgres.update_draft(_BOT_NAME, params["booking_id"], players=params["players"])
    _save(chat_id, "step_name", params)
    return _t(lang, "ask_name")


def _handle_step_name(chat_id: str, user_text: str, params: dict) -> str:
    params["customer_name"] = user_text.strip()
    logger.info("[BOOKING:step_name] customer_name=%r — advancing to step_confirm", params["customer_name"])
    postgres.update_draft(_BOT_NAME, params["booking_id"], customer_name=params["customer_name"])
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

    lang = params.get("lang", "ru")

    if any(w in lower for w in _YES):
        logger.info("[BOOKING:step_confirm] YES received — confirming booking. params=%s", params)
        return _confirm_booking(chat_id, sender_phone, params)

    if any(w in lower for w in _NO):
        logger.info("[BOOKING:step_confirm] NO received — cancelling draft + session")
        if params.get("booking_id"):
            postgres.cancel_booking_trial(
                _BOT_NAME, params["booking_id"], actor_type="whatsapp", actor_id=chat_id, reason="user_declined"
            )
        postgres.delete_session(_BOT_NAME, chat_id)
        return _t(lang, "declined")

    logger.info("[BOOKING:step_confirm] unrecognised response=%.80s — re-showing summary", user_text)
    return _format_summary(params) + "\n\n" + _t(lang, "confirm_reshow")


# ---------------------------------------------------------------------------
# Booking confirmation
# ---------------------------------------------------------------------------

def _confirm_booking(chat_id: str, sender_phone: str, params: dict) -> str:
    lang           = params.get("lang", "ru")
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
        postgres.delete_session(_BOT_NAME, chat_id)
        if res["code"] == "SLOT_TAKEN":
            logger.warning("[BOOKING:confirm] slot taken mid-flow for booking_id=%d", booking_id)
            free = booking_logic.get_free_windows()
            return _t(lang, "slot_taken") + booking_logic.format_availability_context(free)
        logger.error("[BOOKING:confirm] request_payment failed: %s", res)
        return _t(lang, "request_payment_error")

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
            upsert_booking_row(booking_row)
        except Exception as e:
            logger.error("Sheets write failed for booking %d: %s", booking_id, e)

    threading.Thread(target=_write_to_sheets, daemon=True).start()
    postgres.delete_session(_BOT_NAME, chat_id)

    return _t(
        lang,
        "booking_pending",
        date=_fmt_date(params["date"], lang),
        start=time_start_str,
        end=time_end_str,
        field=field,
        fmt=params["format"],
        players=params.get("players"),
        name=params.get("customer_name", ""),
        pay_url=config.KASPI_PAYMENT_URL,
    )


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _ask_date(available_days: list[date], lang: str = "ru") -> str:
    lines = [_t(lang, "ask_date_header")]
    for i, d in enumerate(available_days, 1):
        lines.append(f"  {i}. {_WEEKDAY[lang][d.weekday()]} {d.strftime('%d.%m.%Y')}")
    return "\n".join(lines)


def _ask_time(chosen_date: date, day_windows: list[dict], lang: str = "ru") -> str:
    field_label = _t(lang, "field_label")
    lines = [f"📅 {_WEEKDAY[lang][chosen_date.weekday()]} {chosen_date.strftime('%d.%m.%Y')}\n"]
    lines.append(_t(lang, "ask_time_header"))

    by_field: dict = {}
    for w in day_windows:
        by_field.setdefault((w["field"], w["format"]), []).append(w)

    for (field_id, fmt) in sorted(by_field):
        windows = sorted(by_field[(field_id, fmt)], key=lambda w: w["time_start"])
        range_str = ", ".join(
            f"{w['time_start'].strftime('%H:%M')}–{w['time_end'].strftime('%H:%M')}"
            for w in windows
        )
        lines.append(f"  {field_label} {field_id} ({fmt}): {range_str}")

    lines.append("\n" + _t(lang, "ask_time_prompt"))
    lines.append(_t(lang, "ask_time_example"))
    return "\n".join(lines)


def _ask_field(free_fields: list[dict], lang: str = "ru") -> str:
    field_label = _t(lang, "field_label")
    lines = [_t(lang, "ask_field_header")]
    for f in free_fields:
        lines.append(f"  {f['id']}. {field_label} {f['id']} ({f['format']})")
    return "\n".join(lines)


def _format_summary(params: dict) -> str:
    lang = params.get("lang", "ru")
    return _t(
        lang,
        "summary",
        date=_fmt_date(params.get("date", ""), lang),
        start=params.get("time_start", "?"),
        end=params.get("time_end", "?"),
        field=params.get("field", "?"),
        fmt=params.get("format", "?"),
        players=params.get("players", "?"),
        name=params.get("customer_name", "?"),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_date(date_str: str, lang: str = "ru") -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        return f"{_WEEKDAY[lang][d.weekday()]} {d.strftime('%d.%m.%Y')}"
    except (ValueError, TypeError):
        return date_str or "?"


def _pad_time(t: str) -> str:
    """Ensure HH:MM format (zero-pad single-digit hours)."""
    h, m = t.split(":")
    return f"{int(h):02d}:{m}"

