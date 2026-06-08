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
from datetime import date

import config
from handlers.sessions.base_session import BasePromptBuilder, BaseStepHandler
from integrations import booking as booking_logic
from integrations import booking_service
from integrations.repo import booking_repo, postgres
from integrations.sheets.booking_sheets import upsert_booking_row

logger = logging.getLogger(__name__)


_BOT_NAME = config.BOT_CONFIGS[config.WHATSAPP_PHONE_NUMBER_ID_BOT_1]['name']


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


_LOGGER_MESSAGES = {
    "step_confirm_yes": "[BOOKING:step_confirm] YES received — confirming booking. params=%s",
    "step_confirm_no": "[BOOKING:step_confirm] NO received — cancelling draft + session",
    "step_confirm_unrecognized": "[BOOKING:step_confirm] unrecognised response=%.80s — re-showing summary",
    "step_date_info": "[BOOKING:step_date] available_days=%s | user_text=%.80s",
    "step_date_numeric": "[BOOKING:step_date] numeric input=%s idx=%d chosen=%s",
    "step_date_parse": "[BOOKING:step_date] parsed date via fmt=%s → %s",
    "step_date_parse_2": "[BOOKING:step_date] parsed dd.mm → %s",
    "step_date_rejected": "[BOOKING:step_date] REJECTED — chosen=%s not in available_days=%s",
    "step_date_accepted": "[BOOKING:step_date] ACCEPTED date=%s — %d free windows → advancing to step_time",
    "step_name": "[BOOKING:step_name] customer_name=%r — advancing to step_confirm",
    "step_time_info": "[BOOKING:step_time] date=%s free_windows=%s | user_text=%.80s",
    "step_time_reject_regex": "[BOOKING:step_time] REJECTED — regex did not match user_text=%.80s",
    "step_time_parse": "[BOOKING:step_time] parsed time_start=%s time_end=%s",
    "step_time_reject_inverted": "[BOOKING:step_time] REJECTED — inverted range %s >= %s",
    "step_time_booked": "[BOOKING:step_time] booked slots for week %s–%s: %d total",
    "step_time_fields": "[BOOKING:step_time] free_fields for %s %s–%s: %s",
    "step_time_fields_reject": "[BOOKING:step_time] REJECTED — no free fields for requested time",
    "step_time_advance": "[BOOKING:step_time] multiple free fields=%s — advancing to step_field",
    "step_players_reject": "[BOOKING:step_players] REJECTED — no digit found in user_text=%.80s",
    "step_players_advance": "[BOOKING:step_players] players=%d — advancing to step_name"
}

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


def start_booking_flow(chat_id: str, sender_phone: str, lang: str = "ru") -> str:
    """
    Create a new booking session (step_date) and return the date-selection prompt.
    Called from message_handler when the LLM calls the start_booking tool, or
    directly from handle_booking_turn on a deterministic booking-intent match.
    `lang` is stored in the session so every subsequent step reuses it.
    """
    builder = BookingPromptBuilder(_BOT_NAME)
    handler = BookingStepHandler(_BOT_NAME)
    free = booking_logic.get_free_windows()
    available_days = sorted({w["date"] for w in free})
    logger.info(
        "[BOOKING:start_flow] chat_id=%s lang=%s free_windows=%d available_days=%s",
        chat_id, lang, len(free), available_days,
    )

    if not available_days:
        logger.warning("[BOOKING:start_flow] No available days — aborting flow")
        return builder.data_localization(lang, "no_availability")

    client_token = str(uuid.uuid4())
    draft = postgres.create_draft(bot_name=_BOT_NAME, chat_id=chat_id, phone=sender_phone, client_token=client_token)
    booking_id = draft["data"]["booking_id"]

    handler.save_session(
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
    return builder.ask_date(available_days, lang)


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
    builder = BookingPromptBuilder(_BOT_NAME)
    handler = BookingStepHandler(_BOT_NAME)

    # ── Active session — dispatch to step handler ────────────────────────
    if session:
        state  = session["state"]
        params = session["params"]
        logger.info(
            "[BOOKING] Active session found: chat_id=%s state=%s params=%s | user_text=%.80s",
            chat_id, state, params, user_text,
        )

        # Natural-language cancel at any step ("передумал", "отмена", "не хотим"…)
        if builder.is_cancel_intent(user_text):
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
            return start_booking_flow(chat_id, sender_phone, builder.detect_lang(user_text))

        if state == "step_date":
            return handler.handle_step_date(chat_id, user_text, params)
        if state == "step_time":
            return handler.handle_step_time(chat_id, user_text, params)
        if state == "step_field":
            return handler.handle_step_field(chat_id, user_text, params)
        if state == "step_players":
            return handler.handle_step_players(chat_id, user_text, params)
        if state == "step_name":
            return handler.handle_step_name(chat_id, user_text, params)
        if state == "step_confirm":
            return handler.handle_step_confirm(chat_id, phone_number_id, sender_phone, user_text, params, "booking_id")
        logger.warning("[BOOKING] Unknown session state=%s — falling through", state)
        return None

    # ── No active session — check intents ───────────────────────────────
    # new_booking intent is handled by the LLM via [BOOK] tag (see message_handler.py).
    # Only intercept my_booking here — it requires injecting user-specific data
    # that the LLM cannot fetch on its own.
    from chat.llm import get_booking_reply

    intent = builder.detect_intent(user_text)
    logger.info("[BOOKING] No active session. intent=%s | user_text=%.80s", intent, user_text)

    if intent == "my_booking":
        bookings = booking_repo.get_user_upcoming_bookings(sender_phone)
        logger.info("[BOOKING] my_booking query — %d bookings found for %s", len(bookings), sender_phone)
        ctx = booking_logic.format_user_booking_context(bookings)
        return get_booking_reply(user_text, ctx)

    if intent == "new_booking":
        lang = builder.detect_lang(user_text)
        logger.info("[BOOKING] new_booking intent — starting deterministic flow (lang=%s)", lang)
        return start_booking_flow(chat_id, sender_phone, lang)

    # availability and other intents fall through to the RAG/LLM pipeline.
    # The LLM may still call the start_booking tool for phrasings the keyword
    # list doesn't catch.
    return None


class BookingStepHandler(BaseStepHandler):
    def __init__(self, bot_name: str):
        self.builder = BookingPromptBuilder(bot_name)
        super().__init__(logger_messages=_LOGGER_MESSAGES, builder=self.builder)

    def get_free_now(self, days: list | None = None):
        return booking_logic.get_free_windows()

    def handle_step_name(self, chat_id: str, user_text: str, params: dict) -> str:
        params["customer_name"] = user_text.strip()
        logger.info(self.LOGGER_MESSAGES["step_name"], params["customer_name"])
        postgres.update_draft(self.builder.bot_name, params["booking_id"], customer_name=params["customer_name"])
        self.save_session(chat_id, "step_confirm", params)
        return self.builder.format_summary(params)

    def handle_step_time(self, chat_id: str, user_text: str, params: dict) -> str:
        helper_response = self.step_time_helper(user_text, params)
        if not helper_response["ok"]:
            return helper_response["response"]

        time_start, time_end = helper_response["data"]["time_start"], helper_response["data"]["time_end"]
        day_windows = helper_response["data"]["day_windows"]
        chosen_date = helper_response["data"]["chosen_date"]
        lang = params.get("lang", "ru")

        week_start, week_end = booking_logic.get_week_range()
        booked = booking_logic.get_all_booked(week_start, week_end)
        logger.info(
            self.LOGGER_MESSAGES["step_time_booked"],
            week_start, week_end, len(booked),
        )

        free_fields = [
            f for f in config.BOOKING_FIELDS
            if booking_logic.is_range_free(booked, params["date"], time_start, time_end, f["id"])
        ]
        logger.info(
            self.LOGGER_MESSAGES["step_time_fields"],
            params["date"], time_start, time_end,
            [f["id"] for f in free_fields],
        )

        if not free_fields:
            logger.info(self.LOGGER_MESSAGES["step_time_fields_reject"])
            return (
                    f"{self.builder.data_localization(lang, "no_free_fields", start=time_start, end=time_end)}"
                    f"\n\n{self.builder.ask_time(chosen_date, day_windows, lang)}"
            )

        params["time_start"] = time_start
        params["time_end"] = time_end

        if len(free_fields) == 1:
            f = free_fields[0]
            params["field"] = f["id"]
            params["format"] = f["format"]
            logger.info("[BOOKING:step_time] single free field=%d — advancing to step_players", f["id"])
            postgres.update_draft(
                _BOT_NAME, params["booking_id"], time_start=time_start, time_end=time_end,
                field=f["id"], format=f["format"],
            )
            self.save_session(chat_id, "step_players", params)
            return self.builder.data_localization(lang, "field_free_advance", id=f["id"], fmt=f["format"])

        logger.info(self.LOGGER_MESSAGES["step_time_advance"],
                    [f["id"] for f in free_fields])
        postgres.update_draft(self.builder.bot_name, params["booking_id"], time_start=time_start, time_end=time_end)
        self.save_session(chat_id, "step_field", params)
        return self.builder.ask_field(free_fields, lang)

    def handle_step_field(self, chat_id: str, user_text: str, params: dict) -> str:
        lang = params.get("lang", "ru")
        week_start, week_end = booking_logic.get_week_range()
        booked = booking_logic.get_all_booked(week_start, week_end)
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
            return self.builder.ask_field(free_fields, lang) + "\n\n" + self.builder.data_localization(lang, "ask_field_invalid")

        params["field"] = chosen_field["id"]
        params["format"] = chosen_field["format"]
        postgres.update_draft(
            self.builder.bot_name, params["booking_id"], field=chosen_field["id"], format=chosen_field["format"]
        )
        self.save_session(chat_id, "step_players", params)
        return self.builder.data_localization(lang, "ask_players")

    def handle_step_players(self, chat_id: str, user_text: str, params: dict) -> str:
        lang = params.get("lang", "ru")
        m = re.search(r"\b(\d+)\b", user_text)
        if not m:
            logger.info(self.LOGGER_MESSAGES["step_players_reject"], user_text)
            return self.builder.data_localization(lang, "ask_players_invalid")

        params["players"] = int(m.group(1))
        logger.info(self.LOGGER_MESSAGES["step_players_advance"], params["players"])
        postgres.update_draft(self.builder.bot_name, params["booking_id"], players=params["players"])
        self.save_session(chat_id, "step_name", params)
        return self.builder.data_localization(lang, "ask_name")


class BookingPromptBuilder(BasePromptBuilder):
    def __init__(self, bot_name):
        super().__init__(
            local_dict=_T,
            bot_name=bot_name,
            new_kw=_NEW_BOOKING_KW,
            my_kw=_MY_BOOKING_KW
        )

    def confirm(self, chat_id: str, sender_phone: str, params: dict) -> str:
        lang = params.get("lang", "ru")
        time_start_str = params["time_start"]
        time_end_str = params["time_end"]
        field = int(params["field"])
        booking_id = params["booking_id"]

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
                return self.data_localization(lang, "slot_taken") + booking_logic.format_availability_context(free)
            logger.error("[BOOKING:confirm] request_payment failed: %s", res)
            return self.data_localization(lang, "request_payment_error")

        logger.info("[BOOKING:confirm] booking_id=%d → awaiting_payment", booking_id)

        booking_row = {
            "id": booking_id,
            "field": field,
            "date": params["date"],
            "time_start": time_start_str,
            "time_end": time_end_str,
            "customer_name": params.get("customer_name", ""),
            "phone": sender_phone,
            "players": params.get("players"),
            "state": "ожидание оплаты",
            "notes": "",
        }

        def _write_to_sheets():
            try:
                upsert_booking_row(booking_row)
            except Exception as e:
                logger.error("Sheets write failed for booking %d: %s", booking_id, e)

        threading.Thread(target=_write_to_sheets, daemon=True).start()
        postgres.delete_session(self.bot_name, chat_id)

        return self.data_localization(
            lang,
            "booking_pending",
            date=self.fmt_date(params["date"], lang),
            start=time_start_str,
            end=time_end_str,
            field=field,
            fmt=params["format"],
            players=params.get("players"),
            name=params.get("customer_name", ""),
            pay_url=config.KASPI_PAYMENT_URL,
        )

    def ask_field(self, free_fields: list[dict], lang: str = "ru") -> str:
        field_label = self.data_localization(lang, "field_label")
        lines = [self.data_localization(lang, "ask_field_header")]
        for f in free_fields:
            lines.append(f"  {f['id']}. {field_label} {f['id']} ({f['format']})")
        return "\n".join(lines)

    def ask_time(self, chosen_date: date, day_windows: list[dict], lang: str = "ru") -> str:
        field_label = self.data_localization(lang, "field_label")

        lines = self.format_ask_time(chosen_date, lang)

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

        lines.append("\n" + self.data_localization(lang, "ask_time_prompt"))
        lines.append(self.data_localization(lang, "ask_time_example"))
        return "\n".join(lines)

    def format_summary(self, params: dict) -> str:
        lang = params.get("lang", "ru")
        return self.data_localization(
            lang,
            "summary",
            date=self.fmt_date(params.get("date", ""), lang),
            start=params.get("time_start", "?"),
            end=params.get("time_end", "?"),
            field=params.get("field", "?"),
            fmt=params.get("format", "?"),
            players=params.get("players", "?"),
            name=params.get("customer_name", "?"),
        )
