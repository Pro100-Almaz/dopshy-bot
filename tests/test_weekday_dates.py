from datetime import date

import pytest

from handlers import academy_extractor, extractor
from utils import closest_named_weekday_date, closest_weekday_date, parse_weekdays


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("пт", [4]),
        ("в пятницу", [4]),
        ("жм", [4]),
        ("жұмаға", [4]),
        ("жума", [4]),
        ("дс", [0]),
        ("дуйсенбиге", [0]),
        ("чт немесе жм", [3, 4]),
    ],
)
def test_parse_weekdays_supports_ru_kk_names_and_abbreviations(text, expected):
    assert parse_weekdays(text) == expected


def test_parse_weekdays_does_not_match_aliases_inside_words():
    assert parse_weekdays("средний уровень, всем спасибо") == []


def test_closest_weekday_uses_modulo_and_includes_today():
    sunday = date(2026, 9, 27)

    assert closest_weekday_date(6, today=sunday) == sunday
    assert closest_weekday_date(4, today=sunday) == date(2026, 10, 2)
    assert closest_named_weekday_date("жм", today=sunday) == date(2026, 10, 2)


def test_multiple_named_weekdays_are_not_forced_into_one_date():
    assert closest_named_weekday_date("ср и пт", today=date(2026, 9, 27)) is None


def test_booking_extractor_fills_date_when_llm_misses_short_weekday(monkeypatch):
    monkeypatch.setattr(extractor, "_extract", lambda *args, **kwargs: {"date": None})
    monkeypatch.setattr(extractor, "closest_named_weekday_date", lambda text: date(2026, 10, 2))

    result = extractor.extract_booking_details([], "пт")

    assert result["date"] == "2026-10-02"


def test_academy_extractor_keeps_weekday_when_llm_call_fails(monkeypatch):
    monkeypatch.setattr(
        academy_extractor.client.chat.completions,
        "create",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    monkeypatch.setattr(
        academy_extractor,
        "closest_named_weekday_date",
        lambda text: date(2026, 10, 2),
    )

    result = academy_extractor.extract_trial_details([], "жм")

    assert result["preferred_date"] == "2026-10-02"
    assert result["preferred_weekday"] == 4
