class BaseAsker:
    def __init__(self, T):
        self.T = T

    def localize(self, lang: str, key: str, **kwargs) -> str:
        """Localized string lookup."""
        s = self.T.get(key, {}).get(lang) or self.T.get(key, {}).get("ru", key)
        if kwargs:
            s = s.format(**kwargs)
        return s or ""

    def ask_next_priority(self, data: dict) -> str:
        """Return the question for the highest-priority missing field."""
        lang = data.get("lang", "ru")
        if data.get("date") is None:
            return self.localize(lang, "ask_date")
        if data.get("time_start") is None or data.get("time_end") is None:
            return self.localize(lang, "ask_time")
        if data.get("field") is None:
            return self.localize(lang, "ask_field")
        if data.get("players") is None:
            return self.localize(lang, "ask_players")
        if data.get("customer_name") is None:
            return self.localize(lang, "ask_name")
        return ""

    def ask_date_priority(self, lang: str) -> str:
        """No booking data yet — simply ask for the date."""
        return self.localize(lang, "ask_date")
