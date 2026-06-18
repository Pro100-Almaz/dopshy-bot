import config
from handlers.base_classes.base_button import BaseButton
from handlers.base_classes.base_draft_handler import BaseDraftHandler
from integrations import booking as booking_logic
from handlers.base_classes.base_asker import BaseAsker
from handlers.base_classes.base_format import BaseFormat


class BaseChecker:
    _YES_WORDS = {
        "да", "иә", "ok", "ок", "подтверждаю", "yes",
        "жарайды", "дұрыс", "растаймын", "👍",
    }

    _NO_WORDS = {
        "нет", "жоқ", "no", "отмена", "изменить",
        "өзгерт", "болмайды", "бастапқы", "бас тартамын",
    }

    def __init__(self, asker: BaseAsker, formatter: BaseFormat, buttons: BaseButton, draft_handler: BaseDraftHandler):
        self.asker = asker
        self.formatter = formatter
        self.buttons = buttons
        self.draft_handler = draft_handler

    def check_confirm_response(self, text: str) -> str | None:
        """Return 'yes', 'no', or None from the user's text."""
        lower = text.lower().strip()
        if any(w in lower for w in self._YES_WORDS):
            return "yes"
        if any(w in lower for w in self._NO_WORDS):
            return "no"
        return None

    def check_full_slot(self, data: dict) -> str:
        """Rule 4: date + time + field are all known."""
        lang = data.get("lang", "ru")
        date_str = data["date"]
        ts, te = data["time_start"], data["time_end"]
        field_id = int(data["field"])

        week_start, week_end = booking_logic.get_week_range()
        from datetime import timedelta
        extend = timedelta(days=1) if ts > te else timedelta(0)
        booked = booking_logic.get_all_booked(week_start, week_end + extend)

        field_conf = next(
            (f for f in config.BOOKING_FIELDS if f["id"] == field_id), {},
        )
        fmt = data.get("format") or field_conf.get("format", "?")

        if booking_logic.check_range_free(booked, date_str, ts, te, field_id):
            next_ask = self.asker.ask_next_priority(data)
            if not next_ask:
                return self.check_and_confirm(data)

            return (
                    self.asker.localize(lang, "field_free",
                       fid=field_id, fmt=fmt,
                       date=self.formatter.fmt_date(date_str, lang), ts=ts, te=te)
                    + "\n\n" + next_ask
            )

        free = booking_logic.get_free_windows()
        day_windows = [w for w in free if str(w["date"]) == date_str]
        alt_text = self.formatter.format_windows_by_field(day_windows, lang)

        return (
                self.asker.localize(lang, "field_taken",
                   fid=field_id, fmt=fmt,
                   date=self.formatter.fmt_date(date_str, lang), ts=ts, te=te)
                + "\n\n" + self.asker.localize(lang, "alternatives") + "\n" + alt_text
        )

    def check_date_only(self, data: dict) -> str:
        """Rule 1: show all free fields and their time ranges for the given date."""
        lang = data.get("lang", "ru")
        date_str = data["date"]
        free = booking_logic.get_free_windows()
        day_windows = [w for w in free if str(w["date"]) == date_str]

        if not day_windows:
            return (
                    self.asker.localize(lang, "no_slots_date", date=self.formatter.fmt_date(date_str, lang))
                    + "\n\n" + self.formatter.format_available_dates(free, lang)
            )

        return (
            self.asker.localize(lang, "ask_time_polite", date=self.formatter.fmt_date(date_str, lang))
        )

    def check_time_range_only(self, data: dict) -> str:
        """Rule 2: show available dates and fields for the given time interval."""
        lang = data.get("lang", "ru")
        ts, te = data["time_start"], data["time_end"]

        week_start, week_end = booking_logic.get_week_range()
        from datetime import timedelta
        extend = timedelta(days=1) if ts > te else timedelta(0)
        booked = booking_logic.get_all_booked(week_start, week_end + extend)
        free = booking_logic.get_free_windows()
        dates = sorted({str(w["date"]) for w in free})

        available: list[dict] = []
        for d in dates:
            free_fields = [
                f for f in config.BOOKING_FIELDS
                if booking_logic.check_range_free(booked, d, ts, te, f["id"])
            ]
            if free_fields:
                available.append({"date": d, "fields": free_fields})

        if not available:
            return self.asker.localize(lang, "no_fields_time", ts=ts, te=te)

        return (self.asker.localize(lang, "time_available", ts=ts, te=te)
                 + "\n\n" + self.asker.localize(lang, "which_date"))

    def check_date_and_field(self, data: dict) -> str:
        """Date + field known, time unknown. Show free time ranges."""
        lang = data.get("lang", "ru")
        date_str = data["date"]
        field_id = int(data["field"])
        free = booking_logic.get_free_windows()

        field_conf = next(
            (f for f in config.BOOKING_FIELDS if f["id"] == field_id), {},
        )
        fmt = field_conf.get("format", "?")

        matching_ids = {f["id"] for f in config.BOOKING_FIELDS if f["format"] == fmt}
        windows = [
            w for w in free
            if str(w["date"]) == date_str and w["field"] in matching_ids
        ]

        if not windows:
            day_windows = [w for w in free if str(w["date"]) == date_str]
            alt_text = self.formatter.format_windows_by_field(day_windows, lang)
            return (
                    self.asker.localize(lang, "field_booked_date",
                       fid=field_id, fmt=fmt,
                       date=self.formatter.fmt_date(date_str, lang))
                    + "\n\n" + self.asker.localize(lang, "other_options") + "\n" + alt_text
            )

        intervals = [(w["time_start"], w["time_end"]) for w in windows]
        merged = booking_logic.merge_time_intervals(intervals)
        times_str = ", ".join(
            f"{self.formatter.fmt_time(s)}–{self.formatter.fmt_time(e)}"
            for s, e in merged
        )
        return (
                self.asker.localize(lang, "field_schedule",
                   fid=field_id, fmt=fmt,
                   date=self.formatter.fmt_date(date_str, lang), times=times_str)
                + "\n\n" + self.asker.localize(lang, "write_time")
        )

    def check_field_only(self, data: dict) -> str:
        """Rule 3: show available dates and time ranges for the given format."""
        lang = data.get("lang", "ru")
        field_id = int(data["field"])
        free = booking_logic.get_free_windows()

        field_conf = next(
            (f for f in config.BOOKING_FIELDS if f["id"] == field_id), {},
        )
        fmt = field_conf.get("format", "?")

        matching_ids = {f["id"] for f in config.BOOKING_FIELDS if f["format"] == fmt}
        field_windows = [w for w in free if w["field"] in matching_ids]

        if not field_windows:
            return self.asker.localize(lang, "field_full_week", fid=field_id, fmt=fmt)

        windows_text = self.formatter.format_windows_by_date(field_windows, lang)
        return (
                self.asker.localize(lang, "field_slots", fid=field_id, fmt=fmt)
                + "\n\n" + windows_text
                + "\n\n" + self.asker.localize(lang, "which_date")
        )

    def check_date_and_time(self, data: dict) -> str:
        """Rule 5: date + time are known but field is not. Show available fields."""
        lang = data.get("lang", "ru")
        date_str = data["date"]
        ts, te = data["time_start"], data["time_end"]

        week_start, week_end = booking_logic.get_week_range()
        from datetime import timedelta
        extend = timedelta(days=1) if ts > te else timedelta(0)
        booked = booking_logic.get_all_booked(week_start, week_end + extend)

        candidates = config.BOOKING_FIELDS
        if data.get("format"):
            candidates = [
                f for f in candidates if f["format"] == data["format"]
            ]

        free_fields = [
            f for f in candidates
            if booking_logic.check_range_free(booked, date_str, ts, te, f["id"])
        ]

        if not free_fields:
            free = booking_logic.get_free_windows()
            day_windows = [w for w in free if str(w["date"]) == date_str]
            alt_text = self.formatter.format_windows_by_field(day_windows, lang)
            return (
                    self.asker.localize(lang, "no_free_fields_slot",
                       date=self.formatter.fmt_date(date_str, lang), ts=ts, te=te)
                    + "\n\n" + self.asker.localize(lang, "available_time") + "\n" + alt_text
            )

        # Auto-select if only one format is available
        free_formats = sorted({f["format"] for f in free_fields})
        if len(free_formats) == 1:
            f = free_fields[0]
            data["field"] = f["id"]
            data["format"] = f["format"]
            self.draft_handler.update_draft_in_db(data.get("booking_id"), data)

            next_ask = self.asker.ask_next_priority(data)
            if not next_ask:
                return self.check_and_confirm(data)

            return (
                    self.asker.localize(lang, "field_auto",
                       fid=f["id"], fmt=f["format"],
                       date=self.formatter.fmt_date(date_str, lang), ts=ts, te=te)
                    + "\n\n" + next_ask
            )

        # Multiple formats — let user pick by size
        btn_text = self.asker.localize(lang, "choose_field",
                      date=self.formatter.fmt_date(date_str, lang), ts=ts, te=te)
        return self.buttons.get_buttons(btn_text, free_formats)

    def check_and_confirm(self, data: dict) -> str:
        """
        All 6 fields present. Verify the slot is still free,
        then show the confirmation prompt.
        """
        lang = data.get("lang", "ru")
        date_str = data["date"]
        ts, te = data["time_start"], data["time_end"]
        field_id = int(data["field"])

        week_start, week_end = booking_logic.get_week_range()
        from datetime import timedelta
        extend = timedelta(days=1) if ts > te else timedelta(0)
        booked = booking_logic.get_all_booked(week_start, week_end + extend)

        field_conf = next(
            (f for f in config.BOOKING_FIELDS if f["id"] == field_id), {},
        )
        fmt = data.get("format") or field_conf.get("format", "?")

        if not booking_logic.check_range_free(booked, date_str, ts, te, field_id):
            free = booking_logic.get_free_windows()
            day_windows = [w for w in free if str(w["date"]) == date_str]
            alt_text = self.formatter.format_windows_by_field(day_windows, lang)
            return (
                    self.asker.localize(lang, "slot_taken_confirm",
                       fid=field_id, fmt=fmt,
                       date=self.formatter.fmt_date(date_str, lang), ts=ts, te=te)
                    + "\n\n" + self.asker.localize(lang, "alternatives") + "\n" + alt_text
            )

        summary = (
            f"{self.asker.localize(lang, 'confirm_header')}\n"
            f"📅 {self.formatter.fmt_date(date_str, lang)}\n"
            f"⏰ {ts}–{te}\n"
            f"⚽ {fmt}\n"
            f"👥 {data['players']}\n"
            f"👤 {data['customer_name']}\n\n"
            f"{self.asker.localize(lang, 'confirm_question')}"
        )
        return self.buttons.get_buttons(summary, [
            self.asker.localize(lang, "confirm_btn"),
            self.asker.localize(lang, "cancel_btn"),
        ])
