from datetime import date

import config
from handlers.base_classes import base_checker
from handlers.base_classes.base_checker import BaseChecker


class _Asker:
    def localize(self, lang, key, **kwargs):
        strings = {
            "field_free": "✅ {fmt} бос\n{date} {ts}–{te}!\n💰 {price}",
            "ask_name": "Атыңызды жазыңыз:",
        }
        return strings[key].format(**kwargs)

    def ask_next_priority(self, data):
        return self.localize(data["lang"], "ask_name")


class _Formatter:
    @staticmethod
    def fmt_date(value, lang):
        return value


class _Buttons:
    pass


class _DraftHandler:
    pass


def test_available_slot_prompt_includes_total_price(monkeypatch):
    monkeypatch.setattr(config, "BOOKING_FIELDS", [{"id": 2, "format": "5x5"}])
    monkeypatch.setattr(base_checker.booking_logic, "get_week_range", lambda: (date(2026, 11, 2), date(2026, 11, 8)))
    monkeypatch.setattr(base_checker.booking_logic, "get_all_booked", lambda *_args: [])
    monkeypatch.setattr(base_checker.booking_logic, "check_range_free", lambda *_args: True)
    monkeypatch.setattr(base_checker, "calculate_full_booking_price", lambda *_args: 25_000)

    checker = BaseChecker(_Asker(), _Formatter(), _Buttons(), _DraftHandler())
    response = checker.check_full_slot({
        "lang": "kk",
        "date": "2026-11-07",
        "time_start": "19:30",
        "time_end": "21:30",
        "field": 2,
        "format": "5x5",
        "players": 1,
    })

    assert "💰 25 000 тг" in response
    assert response.index("💰 25 000 тг") < response.index("Атыңызды жазыңыз:")
