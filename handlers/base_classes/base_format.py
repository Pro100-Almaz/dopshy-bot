from handlers.base_classes.base_asker import BaseAsker


class BaseFormat:
    _WEEKDAY = {
        "ru": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
        "kk": ["Дс", "Сс", "Ср", "Бс", "Жм", "Сб", "Жс"],
    }

    def __init__(self, asker: BaseAsker):
        self.asker = asker

    def format_windows_by_field(self, windows: list[dict], lang: str = "ru") -> str:
        """Group free windows by field, format as multiline text."""
        if not windows:
            return self.asker.localize(lang, "no_slots_empty")

        field_label = self.asker.localize(lang, "field_label")
        by_field: dict[tuple, list] = {}
        for w in windows:
            key = (w["field"], w.get("format", "?"))
            by_field.setdefault(key, []).append(w)

        lines = []
        for (fid, fmt) in sorted(by_field):
            sorted_w = sorted(by_field[(fid, fmt)], key=lambda x: x["time_start"])
            times = ", ".join(
                f"{self.fmt_time(w['time_start'])}"
                f"–{self.fmt_time(w['time_end'])}"
                for w in sorted_w
            )
            lines.append(f"  ⚽ {field_label} {fid} ({fmt}): {times}")

        return "\n".join(lines)

    def format_windows_by_date(self, windows: list[dict], lang: str = "ru") -> str:
        """Group free windows by date, format as multiline text."""
        if not windows:
            return self.asker.localize(lang, "no_slots_empty")

        by_date: dict[str, list] = {}
        for w in windows:
            by_date.setdefault(str(w["date"]), []).append(w)

        lines = []
        for d in sorted(by_date):
            sorted_w = sorted(by_date[d], key=lambda x: x["time_start"])
            d_label = self.fmt_date(d, lang)
            times = ", ".join(
                f"{self.fmt_time(w['time_start'])}"
                f"–{self.fmt_time(w['time_end'])}"
                for w in sorted_w
            )
            lines.append(f"  📅 {d_label}: {times}")

        return "\n".join(lines)

    def format_available_dates(self, free_windows: list[dict], lang: str = "ru") -> str:
        """List all dates that have at least one free window."""
        dates = sorted({str(w["date"]) for w in free_windows})
        if not dates:
            return ""

        lines = [self.asker.localize(lang, "available_dates")]
        for d_str in dates:
            lines.append(f"  📅 {self.fmt_date(d_str, lang)}")
        return "\n".join(lines)

    def format_missing_fields(self, data: dict) -> str:
        """Return list of fields that are still None."""
        lang = data.get("lang", "ru")
        missing: list[str] = []

        if data.get("date") is None:
            missing.append("  • " + self.asker.localize(lang, "ask_date").lstrip("📅 "))
        if data.get("time_start") is None or data.get("time_end") is None:
            missing.append("  • " + self.asker.localize(lang, "ask_time").lstrip("⏰ "))
        if data.get("field") is None:
            missing.append("  • " + self.asker.localize(lang, "ask_field").lstrip("⚽ "))
        if data.get("players") is None:
            missing.append("  • " + self.asker.localize(lang, "ask_players").lstrip("👥 "))
        if data.get("customer_name") is None:
            missing.append("  • " + self.asker.localize(lang, "ask_name").lstrip("👤 "))

        return "\n".join(missing)

    def fmt_date(self, date_str: str, lang: str = "ru") -> str:
        """'2026-06-15' → 'Пн 15.06.2026' (ru) or 'Дс 15.06.2026' (kk)"""
        try:
            from datetime import datetime
            dt = datetime.strptime(date_str, "%Y-%m-%d").date()
            return f"{self._WEEKDAY[lang][dt.weekday()]} {dt.strftime('%d.%m.%Y')}"
        except (ValueError, TypeError, KeyError):
            return date_str or "?"

    @staticmethod
    def fmt_time(value) -> str:
        """Accept time objects or 'HH:MM:SS' strings → 'HH:MM'"""
        if hasattr(value, "strftime"):
            return value.strftime("%H:%M")
        return str(value)[:5]
