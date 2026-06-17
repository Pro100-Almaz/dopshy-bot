import json
import logging
import re
from datetime import date, datetime, time

from chat.conversation import clear_history
from integrations.repo import postgres
from utils import today_almaty

logger = logging.getLogger(__name__)


class BasePromptBuilder:
    WEEKDAY = {
        "ru": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
        "kk": ["Дс", "Сс", "Ср", "Бс", "Жм", "Сб", "Жс"],
    }

    KZ_CHARS = set("әғіңөұүһқ")

    CANCEL_PHRASES = (
        "отмен", "стоп", "передум", "не хочу", "не хотим", "не нужно",
        "не надо", "забуд", "забыть", "отбой", "отказыв",
        "тоқтат", "керек емес", "ұнамайды", "болмайды", "бас тарт",
    )

    # Regex to pull two HH:MM times from a single message (e.g. "10:00 до 12:00", "14:30-16:00")
    TIME_RANGE_RE = re.compile(r"(\d{1,2}:\d{2})\s*[-–—до\s]+\s*(\d{1,2}:\d{2})")

    def __init__(
            self,
            local_dict: dict,
            bot_name: str,
            new_kw: tuple,
            my_kw: tuple
    ):
        self.T = local_dict
        self.bot_name = bot_name
        self.NEW_KW = new_kw
        self.MY_KW = my_kw
        self.booking_or_trial = "booking" if self.bot_name == 'dopsy_bot' else "trial"

    def ask_date(self, available_days: list[date], lang: str = "ru") -> str:
        lines = [self.data_localization(lang, "ask_date_header")]

        for i, d in enumerate(available_days, 1):
            lines.append(f"  {i}. {self.WEEKDAY[lang][d.weekday()]} {d.strftime('%d.%m.%Y')}")
        return "\n".join(lines)

    def ask_time(self, chosen_date: date, day_windows: list[dict], lang: str = "ru"):
        return ""

    def confirm(self, chat_id: str, sender_phone: str, params: dict) -> str:
        return ""

    def data_localization(self, lang: str, key: str, **fmt) -> str:
        val = self.T[key].get(lang) or self.T[key]["ru"]
        return val.format(**fmt) if fmt else val

    def detect_intent(self, text: str) -> str | None:
        """
        Deterministic intent detection. my_booking is checked first so phrases like
        "я забронировал" don't get routed to new_booking by the "забронир" substring.

        availability: still handled by the LLM with injected slot data.
        """
        lower = text.lower()
        for kw in self.MY_KW:
            if kw in lower:
                return f"my_{self.booking_or_trial}"
        for kw in self.NEW_KW:
            if kw in lower:
                return f"new_{self.booking_or_trial}"
        return None

    def detect_lang(self, text: str) -> str:
        """Crude lang detection: any Kazakh-only Cyrillic letter → kk, else ru."""
        return "kk" if any(c in self.KZ_CHARS for c in text.lower()) else "ru"

    def fmt_date(self, date_str: str, lang: str = "ru") -> str:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            return f"{self.WEEKDAY[lang][d.weekday()]} {d.strftime('%d.%m.%Y')}"
        except (ValueError, TypeError):
            return date_str or "?"

    @staticmethod
    def fmt_time(value: str | datetime) -> str | time:
        if isinstance(value, str):
            return datetime.strptime(value, "%H:%M").time()
        return datetime.strftime(value, "%H:%M")

    def format_ask_time(self, chosen_date: date, lang: str = "ru"):
        lines = [f"📅 {self.WEEKDAY[lang][chosen_date.weekday()]} {chosen_date.strftime('%d.%m.%Y')}\n",
                 self.data_localization(lang, "ask_time_header")]
        return lines

    def format_summary(self, params: dict, append_message: str | None = None) -> str:
        return ""

    @staticmethod
    def get_buttons(formatted_response, buttons_list: list) -> str:
        buttons = {
            "type": "button",
            "body": {"text": formatted_response},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": f"id_{index}",
                            "title": button
                        }
                    } for index, button in enumerate(buttons_list, start=1)
                ]
            }
        }
        return json.dumps(buttons)

    def is_cancel_intent(self, text: str) -> bool:
        low = text.lower()
        return any(p in low for p in self.CANCEL_PHRASES)

    @staticmethod
    def pad_time(t: str) -> str:
        """Ensure HH:MM format (zero-pad single-digit hours)."""
        h, m = t.split(":")
        return f"{int(h):02d}:{m}"


class BaseStepHandler:
    YES = {"да", "иә", "ok", "ок", "подтверждаю", "yes", "жарайды", "дұрыс", "растаймын", "👍"}
    NO = {"нет", "жоқ", "no", "отмена", "изменить", "өзгерт", "болмайды", "бастапқы", "бас тартамын"}

    WEEKDAY_ALIASES = [
        ["понедельник", "пн", "дүйсенбі", "дс", "дүйсенбіге"],
        ["вторник", "вт", "сейсенбі", "сс", "сейсенбіге"],
        ["среда", "ср", "сәрсенбі", "сәрсенбіге", "среду"],
        ["четверг", "чт", "бейсенбі", "бс", "бейсенбіге"],
        ["пятница", "пятницу", "пят", "пт", "жұма", "жм", "жұмаға"],
        ["суббота", "субботу", "суб", "сб", "сенбі", "сенбіге"],
        ["воскресенье", "воскр", "вс", "жексенбі", "жс", "жексенбіге"],
    ]

    def __init__(
            self, logger_messages: dict[str, str],
            builder: BasePromptBuilder,
    ):
        self.LOGGER_MESSAGES = logger_messages
        self.builder = builder

    def check_confirm_message(self, user_text: str) -> str:
        lower = user_text.lower().strip()
        if any(w in lower for w in self.YES):
            return "yes"
        if any(w in lower for w in self.NO):
            return "no"
        return ""

    def get_free_now(self, days: list | None = None):
        return {}

    def handle_step_confirm(
            self,
            chat_id: str,
            phone_number_id: str,
            sender_phone: str,
            user_text: str,
            params: dict,
            id_type: str,  # trial_id or booking_id
    ) -> str:
        yes_no = self.check_confirm_message(user_text)
        lang = params.get("lang", "ru")

        if yes_no == "yes":
            logger.info(self.LOGGER_MESSAGES["step_confirm_yes"], params)
            return self.builder.confirm(chat_id, sender_phone, params)

        if yes_no == "no":
            logger.info(self.LOGGER_MESSAGES["step_confirm_no"])
            if params.get(id_type):
                postgres.cancel_booking_trial(
                    self.builder.bot_name,
                    object_id=params[id_type],
                    actor_type="whatsapp",
                    actor_id=chat_id,
                    reason="user_declined"
                )
            postgres.delete_session(self.builder.bot_name, chat_id)
            clear_history(chat_id)
            return self.builder.data_localization(lang, "declined")

        logger.info(self.LOGGER_MESSAGES["step_confirm_unrecognized"], user_text)

        reshow_message = "\n\n" + self.builder.data_localization(lang, "confirm_reshow")
        return self.builder.format_summary(params, reshow_message)

    def handle_step_date(self, chat_id: str, user_text: str, params: dict) -> str:
        lang = params.get("lang", "ru")
        # Always recompute available_days here so a session that crossed midnight
        # doesn't keep offering yesterday's date.
        free_now = self.get_free_now()
        available_days = sorted({w["date"] for w in free_now})
        params["available_days"] = [str(d) for d in available_days]
        logger.info(self.LOGGER_MESSAGES["step_date_info"], available_days, user_text)

        chosen: date | None = None
        text = user_text.strip()

        if text.isdigit():
            idx = int(text) - 1
            if 0 <= idx < len(available_days):
                chosen = available_days[idx]
            logger.info(self.LOGGER_MESSAGES['step_date_numeric'], text, idx, chosen)
        else:
            for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
                try:
                    chosen = datetime.strptime(text, fmt).date()
                    logger.info(self.LOGGER_MESSAGES["step_date_parse"], fmt, chosen)
                    break
                except ValueError:
                    pass
            # Also allow "dd.mm" without year
            if not chosen:
                m = re.match(r"^(\d{1,2})\.(\d{1,2})$", text)
                if m:
                    try:
                        chosen = date(today_almaty().year, int(m.group(2)), int(m.group(1)))
                        logger.info(self.LOGGER_MESSAGES["step_date_parse_2"], chosen)
                    except ValueError:
                        pass
                # check if date is given as weekday
                else:
                    available_days_by_weekday = {i.weekday(): i for i in available_days[:7]}
                    text_words = text.split(" ")
                    for day, alias in enumerate(self.WEEKDAY_ALIASES, 0):
                        for word in text_words:
                            if word in alias:
                                chosen = available_days_by_weekday.get(day, None)
                                break


        if not chosen or chosen not in available_days:
            logger.info(self.LOGGER_MESSAGES["step_date_rejected"], chosen, available_days)
            return self.builder.ask_date(available_days, lang) + "\n\n" + self.builder.data_localization(lang, "ask_date_invalid")

        free = self.get_free_now([chosen.weekday()])
        day_windows = [w for w in free if w["date"] == chosen]
        logger.info(self.LOGGER_MESSAGES["step_date_accepted"],chosen, len(day_windows))

        params["date"] = str(chosen)
        postgres.update_draft(self.builder.bot_name, object_id=params[f"{self.builder.booking_or_trial}_id"], date=str(chosen))
        self.save_session(chat_id, "step_time", params)
        return self.builder.ask_time(chosen, day_windows, lang)

    def handle_step_name(self, chat_id: str, user_text: str, params: dict) -> str:
        return ""

    def handle_step_time(self, chat_id: str, user_text: str, params: dict) -> str:
        return ""

    def save_session(self, chat_id: str, state: str, params: dict) -> None:
        """Persist the session, keeping booking_sessions.booking_id in sync."""
        postgres.upsert_session(
            self.builder.bot_name,
            chat_id=chat_id,
            state=state,
            params=params,
            object_id=params.get(f'{self.builder.booking_or_trial}_id')
        )

    def step_time_helper(self, user_text: str, params: dict) -> dict:
        lang = params.get("lang", "ru")
        chosen_date = datetime.strptime(params["date"], "%Y-%m-%d").date()
        free = self.get_free_now([chosen_date.weekday()])
        day_windows = [w for w in free if w["date"] == chosen_date]
        logger.info(
            self.LOGGER_MESSAGES["step_time_info"],
            chosen_date,
            [(str(w["time_start"]), str(w["time_end"])) for w in day_windows],
            user_text,
        )

        m = self.builder.TIME_RANGE_RE.search(user_text)
        if not m:
            logger.info(self.LOGGER_MESSAGES["step_time_reject_regex"], user_text)
            return {
                "ok": False,
                "response": f"""{self.builder.ask_time(chosen_date, day_windows, lang)}\n
                \n{self.builder.data_localization(lang,"time_not_recognized")}"""
            }

        time_start, time_end = m.group(1), m.group(2)
        time_start = self.builder.pad_time(time_start)
        time_end = self.builder.pad_time(time_end)
        logger.info(self.LOGGER_MESSAGES["step_time_parse"], time_start, time_end)

        # TRANSITIVE BOOKING: time_start > time_end is allowed (day transition, e.g. 23:00→01:00)
        # Only reject zero-duration bookings (time_start == time_end)
        if time_start == time_end:
            logger.info(self.LOGGER_MESSAGES["step_time_reject_inverted"], time_start, time_end)
            return {
                "ok": False,
                "response": f"{self.builder.data_localization(lang, "time_inverted")}\n\n" +
                                f"{self.builder.ask_time(chosen_date, day_windows, lang)}"
            }
        return {
                "ok": True,
                "data": {
                    "time_start": time_start,
                    "time_end": time_end,
                    "day_windows": day_windows,
                    "chosen_date": chosen_date,
                    "is_transitive": time_start > time_end,  # TRANSITIVE BOOKING flag
                }
            }
