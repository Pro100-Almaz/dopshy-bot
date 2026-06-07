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
from integrations import trial as trial_logic
from integrations import booking_service, sheets
from integrations.repo import postgres
from utils import today_almaty

from integrations.trial import get_trial_daytime

logger = logging.getLogger(__name__)

_WEEKDAY = {
    "ru": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
    "kk": ["Дс", "Сс", "Ср", "Бс", "Жм", "Сб", "Жс"],
}
# Cyrillic letters that exist only in Kazakh — presence flips the session lang to kk.
_KZ_CHARS = set("әғіңөұүһқ")


def _detect_lang(text: str) -> str:
    """Crude lang detection: any Kazakh-only Cyrillic letter → kk, else ru."""
    return "kk" if any(c in _KZ_CHARS for c in text.lower()) else "ru"


_T = {
    "ask_date_header":        {"ru": "📅 Выберите дату (введите номер):",
                                "kk": "📅 Күнді таңдаңыз (нөмірді енгізіңіз):"},
    "ask_date_invalid":       {"ru": "Пожалуйста, введите номер из списка.",
                                "kk": "Тізімдегі нөмірді енгізіңіз."},
    "ask_time_header":        {"ru": "Доступные время пробных занятии:",
                                "kk": "Қатысып көру сабағының бос уақыты:"},
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


def _save(chat_id: str, state: str, params: dict, bot_name: str) -> None:
    """Persist the session, keeping booking_sessions.booking_id in sync."""
    postgres.upsert_session(bot_name, chat_id=chat_id, state=state, params=params, object_id=params.get("trial_id"))

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

# Substrings (lowercased) that mean "user wants to make a new booking right now".
# Checked AFTER _MY_BOOKING_KW so phrases like "я забронировал" stay on my_booking.
_NEW_TRIAL_KW = (
    "записат", "хочу запи", "хочу про", "проб", "снять поле",
    "хочу зани", "хочу прой",
    "жазылу", "тегін", "қатыс", "келу", "көру",
)

# Substrings (matched in lowercased message) that abort an in-flight booking
# session at any step. Strong cancel intents only — short tokens like "нет" are
# intentionally excluded because they're valid step_confirm inputs.


def detect_intent(text: str) -> str | None:
    """
    Deterministic intent detection. my_booking is checked first so phrases like
    "я забронировал" don't get routed to new_booking by the "забронир" substring.

    availability: still handled by the LLM with injected slot data.
    """
    lower = text.lower()
    for kw in _NEW_TRIAL_KW:
        if kw in lower:
            return "new_booking"
    return None


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def start_trial_flow(chat_id: str, sender_phone: str, bot_name: str, lang: str = "ru") -> str:
    """
    Create a new booking session (step_date) and return the date-selection prompt.
    Called from message_handler when the LLM calls the start_booking tool, or
    directly from handle_booking_turn on a deterministic booking-intent match.
    `lang` is stored in the session so every subsequent step reuses it.
    """
    free = get_trial_daytime(bot_name, None)
    available_days = sorted({w["date"] for w in free})
    logger.info(
        "[TRIAL:start_flow] chat_id=%s lang=%s free_windows=%d available_days=%s",
        chat_id, lang, len(free), available_days,
    )

    if not available_days:
        logger.warning("[TRIAL:start_flow] No available days — aborting flow")
        return _t(lang, "no_availability")

    client_token = str(uuid.uuid4())
    draft = postgres.create_draft(bot_name, chat_id=chat_id, phone=sender_phone, client_token=client_token)
    trial_id = draft["data"]["trial_id"]

    _save(
        chat_id,
        "step_date",
        {
            "sender_phone": sender_phone,
            "available_days": [str(d) for d in available_days],
            "trial_id": trial_id,
            "client_token": client_token,
            "lang": lang,
        },
        bot_name,
    )
    logger.info(
        "[TRIAL:start_flow] Draft booking_id=%d created — step_date. Showing %d days",
        trial_id, len(available_days),
    )
    return _ask_date(available_days, lang)


def handle_trial_turn(
    chat_id: str,
    phone_number_id: str,
    sender_phone: str,
    user_text: str,
    bot_name: str,
) -> str | None:
    """
    Handle one message turn for booking-related flows.

    Returns a reply string if this turn was handled, or None to fall
    through to the regular RAG/LLM pipeline.
    """
    session = postgres.get_active_session(bot_name, chat_id)

    # ── Active session — dispatch to step handler ────────────────────────
    if session:
        state  = session["state"]
        params = session["params"]
        logger.info(
            "[TRIAL] Active session found: chat_id=%s state=%s params=%s | user_text=%.80s",
            chat_id, state, params, user_text,
        )

        # Legacy session from before the state-machine upgrade has no draft booking_id.
        # Discard it and restart the flow cleanly rather than crashing.
        if "trial_id" not in params:
            logger.warning("[TRIAL] Stale session without booking_id — restarting flow for %s", chat_id)
            postgres.delete_session(chat_id, bot_name)
            return start_trial_flow(chat_id, sender_phone, bot_name, _detect_lang(user_text))

        if state == "step_date":
            return _handle_step_date(chat_id, user_text, params, bot_name)
        if state == "step_time":
            return _handle_step_time(chat_id, user_text, params, bot_name)
        if state == "step_name":
            return _handle_step_name(chat_id, user_text, params, bot_name)
        if state == "step_age":
            return _handle_step_age(chat_id, user_text, params, bot_name)
        if state == "step_confirm":
            return _handle_step_confirm(chat_id, phone_number_id, sender_phone, user_text, params, bot_name)
        logger.warning("[TRIAL] Unknown session state=%s — falling through", state)
        return None

    # ── No active session — check intents ───────────────────────────────
    # new_booking intent is handled by the LLM via [BOOK] tag (see message_handler.py).
    # Only intercept my_booking here — it requires injecting user-specific data
    # that the LLM cannot fetch on its own.

    intent = detect_intent(user_text)
    logger.info("[TRIAL] No active session. intent=%s | user_text=%.80s", intent, user_text)


    if intent == "new_booking":
        lang = _detect_lang(user_text)
        logger.info("[TRIAL] new_booking intent — starting deterministic flow (lang=%s)", lang)
        return start_trial_flow(chat_id, sender_phone, bot_name, lang)

    # availability and other intents fall through to the RAG/LLM pipeline.
    # The LLM may still call the start_booking tool for phrasings the keyword
    # list doesn't catch.
    return None


# ---------------------------------------------------------------------------
# Step handlers
# ---------------------------------------------------------------------------

def _handle_step_date(chat_id: str, user_text: str, params: dict, bot_name: str) -> str:
    lang = params.get("lang", "ru")
    # Always recompute available_days here so a session that crossed midnight
    # doesn't keep offering yesterday's date.
    free_now = trial_logic.get_trial_daytime(bot_name, None)
    available_days = sorted({w["date"] for w in free_now})
    params["available_days"] = [str(d) for d in available_days]
    logger.info("[TRIAL:step_date] available_days=%s | user_text=%.80s", available_days, user_text)

    chosen: date | None = None
    text = user_text.strip()

    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(available_days):
            chosen = available_days[idx]
        logger.info("[TRIAL:step_date] numeric input=%s idx=%d chosen=%s", text, idx, chosen)
    else:
        for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                chosen = datetime.strptime(text, fmt).date()
                logger.info("[TRIAL:step_date] parsed date via fmt=%s → %s", fmt, chosen)
                break
            except ValueError:
                pass
        # Also allow "dd.mm" without year
        if not chosen:
            m = re.match(r"^(\d{1,2})\.(\d{1,2})$", text)
            if m:
                try:
                    chosen = date(today_almaty().year, int(m.group(2)), int(m.group(1)))
                    logger.info("[TRIAL:step_date] parsed dd.mm → %s", chosen)
                except ValueError:
                    pass

    if not chosen or chosen not in available_days:
        logger.info(
            "[TRIAL:step_date] REJECTED — chosen=%s not in available_days=%s",
            chosen, available_days,
        )
        return _ask_date(available_days, lang) + "\n\n" + _t(lang, "ask_date_invalid")

    free = trial_logic.get_trial_daytime(bot_name, [chosen])
    day_windows = [w for w in free if w["date"] == chosen]
    logger.info(
        "[TRIAL:step_date] ACCEPTED date=%s — %d free windows → advancing to step_time",
        chosen, len(day_windows),
    )

    params["date"] = str(chosen)
    postgres.update_draft(bot_name, object_id=params["trial_id"], date=str(chosen))
    _save(chat_id, "step_time", params, bot_name)
    return _ask_time(chosen, day_windows, lang)


def _handle_step_time(chat_id: str, user_text: str, params: dict, bot_name) -> str:
    lang = params.get("lang", "ru")
    chosen_date = datetime.strptime(params["date"], "%Y-%m-%d").date()
    free        = trial_logic.get_trial_daytime(bot_name, [chosen_date])
    day_windows = [w for w in free if w["date"] == chosen_date]
    logger.info(
        "[TRIAL:step_time] date=%s free_windows=%s | user_text=%.80s",
        chosen_date,
        [(w["field"], str(w["time_start"]), str(w["time_end"])) for w in day_windows],
        user_text,
    )

    m = _TIME_RANGE_RE.search(user_text)
    if not m:
        logger.info("[TRIAL:step_time] REJECTED — regex did not match user_text=%.80s", user_text)
        return _ask_time(chosen_date, day_windows, lang) + "\n\n" + _t(lang, "time_not_recognized")

    time_start, time_end = m.group(1), m.group(2)
    time_start = _pad_time(time_start)
    time_end   = _pad_time(time_end)
    logger.info("[TRIAL:step_time] parsed time_start=%s time_end=%s", time_start, time_end)

    if time_start >= time_end:
        logger.info("[TRIAL:step_time] REJECTED — inverted range %s >= %s", time_start, time_end)
        return _ask_time(chosen_date, day_windows, lang) + "\n\n" + _t(lang, "time_inverted")

    params["time_start"] = time_start
    params["time_end"]   = time_end


    logger.info("[TRIAL:step_time] advancing to step_name")
    postgres.update_draft(bot_name, object_id=params["trial_id"], time_start=time_start, time_end=time_end)
    _save(chat_id, "step_field", params, bot_name)
    return _t(lang, "ask_name")


def _handle_step_name(chat_id: str, user_text: str, params: dict, bot_name) -> str:
    lang = params.get("lang", "ru")
    params["child_name"] = user_text.strip()
    logger.info("[TRIAL:step_name] child_name=%r — advancing to step_age", params["child_name"])
    postgres.update_draft(bot_name, object_id=params["trial_id"], child_name=params["child_name"])
    _save(chat_id, "step_age", params, bot_name=bot_name)
    return _t(lang, "ask_age")


def _handle_step_age(chat_id: str, user_text: str, params: dict, bot_name) -> str:
    params["child_age"] = user_text.strip()
    logger.info("[TRIAL:step_name] child_age=%r — creating trial lesson for %r", params["child_age"], bot_name)
    postgres.update_draft(bot_name, object_id=params["trial_id"], child_age=params["child_age"])
    _save(chat_id, "step_lesson", params, bot_name=bot_name)
    return _format_summary(params)


def _handle_step_confirm(
    chat_id: str,
    phone_number_id: str,
    sender_phone: str,
    user_text: str,
    params: dict,
    bot_name: str,
) -> str:
    lower = user_text.lower().strip()

    _YES = {"да", "иә", "ok", "ок", "подтверждаю", "yes", "жарайды", "дұрыс", "растаймын", "👍"}
    _NO  = {"нет", "жоқ", "no", "отмена", "изменить", "өзгерт", "болмайды", "бастапқы"}

    lang = params.get("lang", "ru")

    if any(w in lower for w in _YES):
        logger.info("[TRIAL:step_confirm] YES received — confirming trial. params=%s", params)
        return _confirm_booking(chat_id, sender_phone, params, bot_name)

    if any(w in lower for w in _NO):
        logger.info("[TRIAL:step_confirm] NO received — cancelling draft + session")
        if params.get("trial_id"):
            postgres.cancel_booking_trial(
                bot_name, object_id=params["trial_id"], actor_type="whatsapp", actor_id=chat_id, reason="user_declined"
            )
        postgres.delete_session(chat_id, bot_name)
        return _t(lang, "declined")

    logger.info("[TRIAL:step_confirm] unrecognised response=%.80s — re-showing summary", user_text)
    return _format_summary(params) + "\n\n" + _t(lang, "confirm_reshow")




# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _confirm_booking(chat_id: str, sender_phone: str, params: dict, bot_name: str) -> str:
    lang           = params.get("lang", "ru")
    time_start_str = params["time_start"]
    time_end_str   = params["time_end"]
    trial_id     = params["trial_id"]


    logger.info("[TRIAL:confirm] trial_id=%d → writing to sheets", trial_id)

    trial_row = {
        "bot_name":      bot_name,
        "id":            trial_id,
        "date":          params["date"],
        "time_start":    time_start_str,
        "time_end":      time_end_str,
        "child_name":    params.get("child_name", ""),
        "phone":         sender_phone,
        "notes":         "",
    }

    def _write_to_sheets():
        try:
            pass
            # sheets.upsert_trial_row(trial_row)
        except Exception as e:
            logger.error("Sheets write failed for trial %d: %s", trial_id, e)

    threading.Thread(target=_write_to_sheets, daemon=True).start()
    postgres.delete_session(chat_id, bot_name)

    return _t(
        lang,
        "trial_pending",
        date=_fmt_date(params["date"], lang),
        start=time_start_str,
        end=time_end_str,
        name=params.get("customer_name", ""),
        age=params["age"],
        pay_url=config.KASPI_PAYMENT_URL,
    )


def _ask_date(available_days: list[date], lang: str = "ru") -> str:
    lines = [_t(lang, "ask_date_header")]
    for i, d in enumerate(available_days, 1):
        lines.append(f"  {i}. {_WEEKDAY[lang][d.weekday()]} {d.strftime('%d.%m.%Y')}")
    return "\n".join(lines)


def _ask_time(chosen_date: date, day_windows: list[dict], lang: str = "ru") -> str:
    field_label = _t(lang, "field_label")
    lines = [f"📅 {_WEEKDAY[lang][chosen_date.weekday()]} {chosen_date.strftime('%d.%m.%Y')}\n"]
    lines.append(_t(lang, "ask_time_header"))

    windows = list({(w["time_start"], w["time_end"]) for w in day_windows})
    windows.sort()

    range_str = ", ".join(
        f"{w[0].strftime('%H:%M')}–{w[1].strftime('%H:%M')}"
        for w in windows
    )
    lines.append(f"  {range_str}")

    lines.append("\n" + _t(lang, "ask_time_prompt"))
    lines.append(_t(lang, "ask_time_example"))
    return "\n".join(lines)


def _format_summary(params: dict) -> str:
    lang = params.get("lang", "ru")
    return _t(
        lang,
        "summary",
        date=_fmt_date(params.get("date", ""), lang),
        start=params.get("time_start", "?"),
        end=params.get("time_end", "?"),
        name=params.get("customer_name", "?"),
        age=params.get("age", "?"),
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


