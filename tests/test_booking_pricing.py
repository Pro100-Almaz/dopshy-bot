from handlers.payment import pricing


TARIFFS = {
    "morning_day": 10_000,
    "evening": 20_000,
    "late_night": 30_000,
    "after_midnight": 40_000,
    "weekday_special": 5_000,
}


def _use_tariffs(monkeypatch):
    monkeypatch.setattr(pricing, "get_prices_for_format", lambda _format: TARIFFS)


def test_standard_booking_total(monkeypatch):
    _use_tariffs(monkeypatch)

    total = pricing.calculate_full_booking_price("6x6", "2026-11-07", "10:00", "11:00")

    assert total == 10_000


def test_weekday_special_booking_total(monkeypatch):
    _use_tariffs(monkeypatch)

    total = pricing.calculate_full_booking_price("6x6", "2026-11-09", "12:00", "14:00")

    assert total == 10_000


def test_multi_tariff_booking_total(monkeypatch):
    _use_tariffs(monkeypatch)

    total = pricing.calculate_full_booking_price("6x6", "2026-11-07", "17:30", "19:30")

    assert total == 30_000


def test_midnight_crossing_booking_total(monkeypatch):
    _use_tariffs(monkeypatch)

    total = pricing.calculate_full_booking_price("6x6", "2026-11-07", "23:00", "01:00")

    assert total == 70_000
