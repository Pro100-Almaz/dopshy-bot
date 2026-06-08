from datetime import date, datetime


class BasePromptBuilder:
    WEEKDAY = {
        "ru": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
        "kk": ["Дс", "Сс", "Ср", "Бс", "Жм", "Сб", "Жс"],
    }

    def __init__(
        self,
        local_dict: dict,
    ):
        self.T = local_dict

    def data_localization(self, lang: str, key: str, **fmt) -> str:
        val = self.T[key].get(lang) or self.T[key]["ru"]
        return val.format(**fmt) if fmt else val

    def ask_date(self, available_days: list[date], lang: str = "ru") -> str:
        lines = [
            self.data_localization(lang, "ask_date_header")
        ]

        for i, d in enumerate(available_days, 1):
            lines.append(f"  {i}. {self.WEEKDAY[lang][d.weekday()]} {d.strftime('%d.%m.%Y')}")
        return "\n".join(lines)

    def ask_time(self, chosen_date: date, day_windows: list[dict], lang: str = "ru"):
        return

    def format_ask_time(self, chosen_date: date, lang: str = "ru"):
        lines = [f"📅 {self.WEEKDAY[lang][chosen_date.weekday()]} {chosen_date.strftime('%d.%m.%Y')}\n",
                 self.data_localization(lang, "ask_time_header")]

        return lines
