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
import uuid
from datetime import date, datetime
from functools import lru_cache

from chat.conversation import clear_history
from handlers.edit_trial import handle_cancel_trial_request
from handlers.sessions.base_session import BasePromptBuilder
from integrations import trial as trial_logic
from integrations.repo import postgres, academy_repo
from integrations.repo.academy_repo import has_active_trial, check_trial_limits
from integrations.sheets.trial_sheets import refresh_all_trials
from integrations.trial import get_trial_daytime
from utils import today_almaty

logger = logging.getLogger(__name__)


_T = {
    "ask_date_header": {"ru": "📅 Выберите дату (введите номер):",
                        "kk": "📅 Күнді таңдаңыз (нөмірді енгізіңіз):"},
    "ask_date_invalid": {"ru": "Пожалуйста, введите номер из списка.",
                         "kk": "Тізімдегі нөмірді енгізіңіз."},
    "ask_time_header": {"ru": "Доступные время пробных занятии:",
                        "kk": "Қатысып көру сабағының бос уақыты:"},
    "ask_time_prompt": {"ru": "Введите время начала и окончания:",
                        "kk": "Басталу және аяқталу уақытын енгізіңіз:"},
    "ask_time_example": {"ru": "Например: *10:00 до 12:00* или *10:20-11:45*",
                         "kk": "Мысалы: *10:00 - 12:00* немесе *10:20-11:45*"},
    "time_not_recognized": {"ru": "Не распознал время. Пример: *10:00 до 12:00*",
                            "kk": "Уақыт танылмады. Мысалы: *10:00 - 12:00*"},
    "time_inverted": {"ru": "Время окончания должно быть позже времени начала. Пример: *10:00 до 12:00*",
                      "kk": "Аяқталу уақыты басталу уақытынан кейін болуы керек. Мысалы: *10:00 - 12:00*"},
    "ask_name": {"ru": "Укажите имя вашего ребенка:",
                 "kk": "Балаңыздың есімін жазыңыз:"},
    "ask_age": {"ru": "Сколько лет вашему ребенку?",
                "kk": "Балаңызды жасы нешеде?"},
    "summary": {
        "ru": "📋 Детали записи:\n📅 {date}\n⏰ {start}–{end}\n👤 Имя ребенка: {child_name}\n🎂 Возраст ребенка: {child_age}\n\nПодтвердить? Ответьте *да* или *нет*.",
        "kk": "📋 Брондау деректері:\n📅 {date}\n⏰ {start}–{end}\n👤 Балаңыздың есімі: {child_name}\n🎂 Балаңыздың жасы: {child_age}\n\nРастайсыз ба? *иә* немесе *жоқ* деп жауап беріңіз."},
    "confirm_reshow": {"ru": "Подтвердить бронь? Ответьте *да* или *нет*.",
                       "kk": "Брондауды растайсыз ба? *иә* немесе *жоқ* деп жауап беріңіз."},
    "declined": {"ru": "Запись отменена. Если захотите снова — просто напишите, что хотите записаться на пробное занятие. 🙂",
                 "kk": "Жазылым тоқтатылды. Қайта қаласаңыз — сынақ сабағына қатысқыңыз келетінін жазыңыз. 🙂"},
    "no_availability": {
        "ru": "К сожалению, свободных слотов на ближайшие 7 дней нет. Пожалуйста, свяжитесь с администратором.",
        "kk": "Өкінішке орай, келесі 7 күнде бос слот жоқ. Әкімшімен хабарласыңыз."},
    "confirmed_trial": {
        "ru": "Вы записаны на пробный урок, будем вас ждать!\n📅 {date}\n⏰ {start}–{end}\n👤 Имя ребенка: {child_name}\n🎂 Возраст ребенка: {child_age}\n",
        "kk": "Жазылым сәтті аяқталды, сізді қуана күтеміз!\n📅 {date}\n⏰ {start}–{end}\n👤 Балаңыздың есімі: {child_name}\n🎂 Балаңыздың жасы: {child_age}\n"},
    "reached_limits": {
        "ru": "Вы достигли максимум попыток пробных занятие",
        "kk": "Сіз сынақ сабағының қатысу саны шегіне жеттіңіз"
    },
    "has_active_trial": {
        "ru": "Вы уже записались на пробное занятие",
        "kk": "Сіз сынақ сабағына тіркелдіңіз"
    },
}

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


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def start_trial_flow(chat_id: str, sender_phone: str, bot_name: str, lang: str = "ru") -> str:
    """
    Create a new trial session (step_date) and return the date-selection prompt.
    Called from message_handler when the LLM calls the start_trial tool, or
    directly from handle_trial_turn on a deterministic trial-intent match.
    `lang` is stored in the session so every subsequent step reuses it.
    """
    builder = get_trial_prompt_builder(bot_name)
    free = get_trial_daytime(bot_name, None)
    available_days = sorted({w["date"] for w in free})
    logger.info(
        "[TRIAL:start_flow] chat_id=%s lang=%s free_windows=%d available_days=%s",
        chat_id, lang, len(free), available_days,
    )

    if not check_trial_limits(bot_name, sender_phone):
        logger.info("[TRIAL: start_flow] User reached trial limits sender_phone=%s, bot_name=%s",
                    sender_phone, bot_name)

    if has_active_trial(bot_name, sender_phone):
        logger.info("[TRIAL: start_flow] User already has confirmed trial sender_phone=%s, bot_name=%s",
                    sender_phone, bot_name)

    if not available_days:
        logger.warning("[TRIAL:start_flow] No available days — aborting flow")
        return builder.data_localization(lang, "no_availability")

    client_token = str(uuid.uuid4())
    draft = postgres.create_draft(bot_name, chat_id=chat_id, phone=sender_phone, client_token=client_token)
    trial_id = draft["data"]["trial_id"]

    builder.save_session(
        chat_id,
        "step_date",
        {
            "sender_phone": sender_phone,
            "available_days": [str(d) for d in available_days],
            "trial_id": trial_id,
            "client_token": client_token,
            "lang": lang,
        },
    )
    logger.info(
        "[TRIAL:start_flow] Draft trial_id=%d created — step_date. Showing %d days",
        trial_id, len(available_days),
    )
    return builder.ask_date(available_days, lang)


def handle_trial_turn(
        chat_id: str,
        phone_number_id: str,
        sender_phone: str,
        user_text: str,
        bot_name: str,
) -> str | None:
    """
    Handle one message turn for trial-related flows.

    Returns a reply string if this turn was handled, or None to fall
    through to the regular RAG/LLM pipeline.
    """
    session = postgres.get_active_session(bot_name, chat_id)
    builder = get_trial_prompt_builder(bot_name)

    # ── Active session — dispatch to step handler ────────────────────────
    if session:
        state = session["state"]
        params = session["params"]
        logger.info(
            "[TRIAL] Active session found: chat_id=%s state=%s params=%s | user_text=%.80s",
            chat_id, state, params, user_text,
        )

        if builder.is_cancel_intent(user_text):
            tid = params.get("trial_id")
            if tid:
                handle_cancel_trial_request(chat_id, sender_phone, bot_name)
                # postgres.cancel_booking_trial(bot_name, tid, actor_id=chat_id)
            else:
                postgres.delete_session(bot_name, chat_id)
            logger.info("[TRIAL] Cancel intent detected — session cleared for %s", chat_id)
            return ("Хорошо, запись отменена. Если передумаете — просто напишите! 🙂\n\n"
                    "Жарайды, брондау тоқтатылды. Қайта қаласаңыз — жазыңыз!")

        # Legacy session from before the state-machine upgrade has no draft trial_id.
        # Discard it and restart the flow cleanly rather than crashing.
        if "trial_id" not in params:
            logger.warning("[TRIAL] Stale session without trial_id — restarting flow for %s", chat_id)
            postgres.delete_session(bot_name, chat_id)
            return start_trial_flow(chat_id, sender_phone, bot_name, builder.detect_lang(user_text))

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
    # new_trial intent is handled by the LLM via [BOOK] tag (see message_handler.py).
    # Only intercept my_trial here — it requires injecting user-specific data
    # that the LLM cannot fetch on its own.

    intent = builder.detect_intent(user_text)
    logger.info("[TRIAL] No active session. intent=%s | user_text=%.80s", intent, user_text)

    if intent == "new_trial":
        lang = builder.detect_lang(user_text)
        logger.info("[TRIAL] new_trial intent — starting deterministic flow (lang=%s)", lang)
        return start_trial_flow(chat_id, sender_phone, bot_name, lang)

    # availability and other intents fall through to the RAG/LLM pipeline.
    # The LLM may still call the start_trial tool for phrasings the keyword
    # list doesn't catch.
    return None


# ---------------------------------------------------------------------------
# Step handlers
# ---------------------------------------------------------------------------

def _handle_step_date(chat_id: str, user_text: str, params: dict, bot_name: str) -> str:
    builder = get_trial_prompt_builder(bot_name)
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
        return builder.ask_date(available_days, lang) + "\n\n" + builder.data_localization(lang, "ask_date_invalid")

    free = trial_logic.get_trial_daytime(bot_name, [chosen.weekday()])
    day_windows = [w for w in free if w["date"] == chosen]
    logger.info(
        "[TRIAL:step_date] ACCEPTED date=%s — %d free windows → advancing to step_time",
        chosen, len(day_windows),
    )

    params["date"] = str(chosen)
    postgres.update_draft(bot_name, object_id=params["trial_id"], date=str(chosen))
    builder.save_session(chat_id, "step_time", params)
    return builder.ask_time(chosen, day_windows, lang)


def _handle_step_time(chat_id: str, user_text: str, params: dict, bot_name) -> str:
    builder = get_trial_prompt_builder(bot_name)
    lang = params.get("lang", "ru")
    chosen_date = datetime.strptime(params["date"], "%Y-%m-%d").date()
    free = trial_logic.get_trial_daytime(bot_name, [chosen_date.weekday()])
    day_windows = [w for w in free if w["date"] == chosen_date]
    print("day_windows:", day_windows)
    print("chosen_date:", chosen_date)
    logger.info(
        "[TRIAL:step_time] date=%s free_windows=%s | user_text=%.80s",
        chosen_date,
        [(str(w["time_start"]), str(w["time_end"])) for w in day_windows],
        user_text,
    )

    m = builder.TIME_RANGE_RE.search(user_text)
    if not m:
        logger.info("[TRIAL:step_time] REJECTED — regex did not match user_text=%.80s", user_text)
        return builder.ask_time(chosen_date, day_windows, lang) + "\n\n" + builder.data_localization(lang, "time_not_recognized")

    time_start, time_end = m.group(1), m.group(2)
    time_start = builder.pad_time(time_start)
    time_end = builder.pad_time(time_end)
    logger.info("[TRIAL:step_time] parsed time_start=%s time_end=%s", time_start, time_end)

    print(builder.fmt_time(time_start), builder.fmt_time(time_end))
    for w in day_windows:
        print(w['time_start'], w['time_end'])

    group_ids = [
        w['group_id'] for w in day_windows
        if w['time_start'] == builder.fmt_time(time_start)
           and w['time_end'] == builder.fmt_time(time_end)
    ]
    if len(group_ids) == 0:
        logger.info("[TRIAL:step_time] REJECTED — time range not found: time_start=%s time_end=%s", time_start,
                    time_end)
        return builder.data_localization(lang, "time_not_recognized") + "\n\n" + builder.ask_time(chosen_date, day_windows, lang)

    if time_start >= time_end:
        logger.info("[TRIAL:step_time] REJECTED — inverted range %s >= %s", time_start, time_end)
        return builder.data_localization(lang, "time_inverted") + "\n\n" + builder.ask_time(chosen_date, day_windows, lang)

    params["time_start"] = time_start
    params["time_end"] = time_end

    logger.info("[TRIAL:step_time] advancing to step_name")
    postgres.update_draft(bot_name, object_id=params["trial_id"], time_start=time_start, time_end=time_end,
                          group_id=group_ids[0])
    builder.save_session(chat_id, "step_name", params)
    return builder.data_localization(lang, "ask_name")


def _handle_step_name(chat_id: str, user_text: str, params: dict, bot_name) -> str:
    builder = get_trial_prompt_builder(bot_name)
    lang = params.get("lang", "ru")
    params["child_name"] = user_text.strip()
    logger.info("[TRIAL:step_name] child_name=%r — advancing to step_age", params["child_name"])
    postgres.update_draft(bot_name, object_id=params["trial_id"], child_name=params["child_name"])
    builder.save_session(chat_id, "step_age", params)
    return builder.data_localization(lang, "ask_age")


def _handle_step_age(chat_id: str, user_text: str, params: dict, bot_name) -> str:
    builder = get_trial_prompt_builder(bot_name)
    params["child_age"] = user_text.strip()
    logger.info("[TRIAL:step_name] child_age=%r — creating trial lesson for %r", params["child_age"], bot_name)
    postgres.update_draft(bot_name, object_id=params["trial_id"], child_age=params["child_age"])
    builder.save_session(chat_id, "step_confirm", params)
    return builder.format_summary(params)


def _handle_step_confirm(
        chat_id: str,
        phone_number_id: str,
        sender_phone: str,
        user_text: str,
        params: dict,
        bot_name: str,
) -> str:
    builder = get_trial_prompt_builder(bot_name)
    lower = user_text.lower().strip()

    _YES = {"да", "иә", "ok", "ок", "подтверждаю", "yes", "жарайды", "дұрыс", "растаймын", "👍"}
    _NO = {"нет", "жоқ", "no", "отмена", "изменить", "өзгерт", "болмайды", "бастапқы"}

    lang = params.get("lang", "ru")

    if any(w in lower for w in _YES):
        logger.info("[TRIAL:step_confirm] YES received — confirming trial. params=%s", params)
        return builder.confirm_trial(chat_id, sender_phone, params, bot_name)

    if any(w in lower for w in _NO):
        logger.info("[TRIAL:step_confirm] NO received — cancelling draft + session")
        if params.get("trial_id"):
            postgres.cancel_booking_trial(
                bot_name, object_id=params["trial_id"], actor_type="whatsapp", actor_id=chat_id, reason="user_declined"
            )
        postgres.delete_session(bot_name, chat_id)
        clear_history(chat_id)
        return builder.data_localization(lang, "declined")

    logger.info("[TRIAL:step_confirm] unrecognised response=%.80s — re-showing summary", user_text)
    return builder.format_summary(params) + "\n\n" + builder.data_localization(lang, "confirm_reshow")


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

class TrialPromptBuilder(BasePromptBuilder):
    def __init__(self, bot_name):
        super().__init__(
            local_dict=_T,
            bot_name=bot_name,
            new_kw=_NEW_TRIAL_KW,
            my_kw=()
        )

    def confirm_trial(self, chat_id: str, sender_phone: str, params: dict, bot_name: str) -> str:
        lang = params.get("lang", "ru")
        time_start_str = params["time_start"]
        time_end_str = params["time_end"]
        trial_date = params.get("date")
        trial_id = params["trial_id"]

        logger.info("[TRIAL:confirm] trial_id=%d → writing to sheets", trial_id)

        trial_row = {
            "trial_day": datetime.strptime(trial_date, '%Y-%m-%d') if trial_date else None,
            "start_time": time_start_str,
            "end_time": time_end_str,
            "child_name": params.get("child_name", ""),
            "phone": sender_phone,
            "child_age": params.get("child_age", ""),
        }

        postgres.update_draft(bot_name, object_id=params["trial_id"], **trial_row)
        academy_repo.confirm_trial(params["trial_id"])
        refresh_all_trials()
        postgres.delete_session(bot_name, chat_id)
        clear_history(chat_id)

        return self.data_localization(
            lang,
            "confirmed_trial",
            date=self.fmt_date(params["date"], lang),
            start=time_start_str,
            end=time_end_str,
            child_name=params.get("child_name", ""),
            child_age=params["child_age"],
        )


    def ask_time(self, chosen_date: date, day_windows: list[dict], lang: str = "ru") -> str:
        lines = self.format_ask_time(chosen_date, lang)

        windows = list({(w["time_start"], w["time_end"]) for w in day_windows})
        windows.sort()

        range_str = "\n".join(
            f"——\t\t\t{w[0].strftime('%H:%M')}–{w[1].strftime('%H:%M')}\t\t\t——"
            for w in windows
        )
        lines.append(f"{range_str}")

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
            child_name=params.get("child_name", "?"),
            child_age=params.get("child_age", "?"),
        )


def get_trial_prompt_builder(bot_name: str) -> TrialPromptBuilder:
    return TrialPromptBuilder(bot_name)
