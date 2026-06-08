import re
from datetime import date, datetime, time

from integrations.repo import postgres


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
        return

    def confirm_booking(self, chat_id: str, sender_phone: str, params: dict):
        return

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

    def is_cancel_intent(self, text: str) -> bool:
        low = text.lower()
        return any(p in low for p in self.CANCEL_PHRASES)

    @staticmethod
    def pad_time(t: str) -> str:
        """Ensure HH:MM format (zero-pad single-digit hours)."""
        h, m = t.split(":")
        return f"{int(h):02d}:{m}"

    def save_session(self, chat_id: str, state: str, params: dict) -> None:
        """Persist the session, keeping booking_sessions.booking_id in sync."""
        postgres.upsert_session(
            self.bot_name,
            chat_id=chat_id,
            state=state,
            params=params,
            object_id=params.get(f'{self.booking_or_trial}_id')
        )


