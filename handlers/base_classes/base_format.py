from handlers.base_classes.base_asker import BaseAsker


class BaseFormat:
    _WEEKDAY = {
        "ru": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
        "kk": ["Дс", "Сс", "Ср", "Бс", "Жм", "Сб", "Жс"],
    }

    def __init__(self, asker: BaseAsker):
        self.asker = asker

    def format_windows_by_field(self, windows: list[dict], lang: str = "ru") -> str:
        """List each free field separately (labeled by format), merging that
        field's own intervals. Two fields of the same format (e.g. two "5x5")
        appear as separate lines."""
        if not windows:
            return self.asker.localize(lang, "no_slots_empty")

        from integrations.booking import merge_time_intervals

        by_field: dict = {}
        for w in windows:
            by_field.setdefault(w.get("field"), []).append(w)

        lines = []
        for field_id in sorted(
            by_field,
            key=lambda fid: (by_field[fid][0].get("format", "?"), fid),
        ):
            field_windows = by_field[field_id]
            fmt = field_windows[0].get("format", "?")
            intervals = [(w["time_start"], w["time_end"]) for w in field_windows]
            merged = merge_time_intervals(intervals)
            times = ", ".join(
                f"{self.fmt_time(s)}–{self.fmt_time(e)}" for s, e in merged
            )
            lines.append(f"  ⚽ {fmt}: {times}")

        return "\n".join(lines)

    def format_windows_by_date(self, windows: list[dict], lang: str = "ru") -> str:
        """Group free windows by date, merge intervals, format as multiline text."""
        if not windows:
            return self.asker.localize(lang, "no_slots_empty")

        from integrations.booking import merge_time_intervals

        by_date: dict[str, list] = {}
        for w in windows:
            by_date.setdefault(str(w["date"]), []).append(w)

        lines = []
        for d in sorted(by_date):
            intervals = [(w["time_start"], w["time_end"]) for w in by_date[d]]
            merged = merge_time_intervals(intervals)
            d_label = self.fmt_date(d, lang)
            times = ", ".join(
                f"{self.fmt_time(s)}–{self.fmt_time(e)}" for s, e in merged
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
