"""Client edge cases: one child under two spellings,
duplicate webhook deliveries, «бүгін сабақ бар ма?», holidays, the address
after signing up."""
from datetime import date, time

import pytest

from handlers import edit_trial, message_batcher
from integrations import trial
from integrations.repo.utils import same_child_name
from utils import parse_relative_weekdays

pytestmark = pytest.mark.no_db

_TODAY = date(2026, 10, 26)  # Monday


# ── One child, two spellings ──

@pytest.mark.parametrize("a, b", [
    ("Алихан", "Әлихан"),
    ("Айсұлтан", "Айсултан"),
    ("Alikhan", "Алихан"),
    (" алихан ", "Алихан"),
    ("Айсултан", "Айсултанн"),
    ("Нұрасыл", "Нурасил"),
])
def test_same_child_under_another_spelling(a, b):
    assert same_child_name(a, b)


@pytest.mark.parametrize("a, b", [
    ("Алихан", "Амина"),
    ("Али", "Аля"),
    ("Айдос", "Айдана"),
    ("Алихан", None),
])
def test_different_children_stay_apart(a, b):
    assert not same_child_name(a, b)


# ── Duplicate webhook deliveries ──

class _FakeRedis:
    def __init__(self):
        self.keys = set()

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True


def _payload(message_id):
    return type("P", (), {"whatsapp_message_id": message_id, "provider_message_id": None})()


def test_second_delivery_of_a_message_is_skipped(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(message_batcher, "_redis", lambda: fake)

    assert message_batcher._is_duplicate(_payload("wamid.1")) is False
    assert message_batcher._is_duplicate(_payload("wamid.1")) is True
    assert message_batcher._is_duplicate(_payload("wamid.2")) is False


def test_duplicate_check_fails_open(monkeypatch):
    def down():
        raise ConnectionError("redis down")
    monkeypatch.setattr(message_batcher, "_redis", down)

    assert message_batcher._is_duplicate(_payload("wamid.1")) is False
    assert message_batcher._is_duplicate(_payload(None)) is False


# ── «Бүгін сабақ бар ма?» ──

@pytest.mark.parametrize("text, expected", [
    ("Бүгін сабақ бар ма?", [0]),
    ("сегодня есть тренировка?", [0]),
    ("ертең жаттығу бола ма", [1]),
    ("а завтра?", [1]),
    ("бүгін немесе ертең", [0, 1]),
    ("послезавтра есть?", []),
    ("сабақ қашан?", []),
])
def test_today_and_tomorrow_are_weekdays(text, expected):
    assert parse_relative_weekdays(text, today=_TODAY) == expected


# ── Holidays ──

_GROUP = {"group_id": 1, "group_name": "G", "training_day": 0, "time_start": time(17, 0),
          "time_end": time(18, 0), "birth_years": [2017], "level": ["Beginner"],
          "max_cap": 20, "curr_cap": 0, "trainer": "Тимур"}


@pytest.fixture
def holiday(monkeypatch):
    """Monday 02.11.2026 is closed; the group trains on Mondays."""
    monkeypatch.setattr(trial.academy_repo, "get_groups_info", lambda bot_name, audience=None: [dict(_GROUP)])
    monkeypatch.setattr(trial, "closest_weekday_date", lambda weekday: date(2026, 11, 2))
    monkeypatch.setattr(trial, "is_past_booking_time", lambda *a: False)
    monkeypatch.setattr(trial.academy_repo, "get_closed_dates",
                        lambda bot_name: {date(2026, 11, 2): "Мереке"})


def test_trial_on_a_closed_day_moves_a_week(holiday):
    [slot] = trial.get_eligible_trial_slots("dopsy_boxing", 2017, "kindergarten")

    assert slot["date"] == date(2026, 11, 9)


def test_schedule_on_a_closed_day_says_no_training(holiday):
    classes = trial.list_classes_by_weekday("dopsy_boxing", [0])
    reply = trial.format_weekday_schedule(classes, "kk")

    assert "Жаттығу болмайды (Мереке)" in reply
    assert "17:00" not in reply


def test_closed_days_reach_the_llm(holiday):
    assert "02.11.2026 — Мереке" in trial.format_closed_dates_context("dopsy_boxing")


def test_without_the_table_every_day_stays_open(holiday, monkeypatch):
    def missing(bot_name):
        raise RuntimeError('relation "academy_closed_dates" does not exist')
    monkeypatch.setattr(trial.academy_repo, "get_closed_dates", missing)

    [slot] = trial.get_eligible_trial_slots("dopsy_boxing", 2017, "kindergarten")

    assert slot["date"] == date(2026, 11, 2)
    assert trial.format_closed_dates_context("dopsy_boxing") == ""


# ── The address after signing up ──

def test_trial_status_has_the_address(monkeypatch):
    monkeypatch.setattr(edit_trial, "_active_trials", lambda bot, phone: [
        {"state": "confirmed", "trial_day": date(2026, 11, 2), "start_time": time(17, 0),
         "end_time": time(18, 0), "child_name": "Алихан"},
    ])
    monkeypatch.setattr(edit_trial, "_trial_details", lambda t, i, lang: "Алихан 02.11 17:00")

    reply = edit_trial.handle_trial_status_request("+77001112233", "dopsy_boxing", "kk")

    assert "Сығанақ 6Ф, екінші қабат" in reply
    assert "2gis.kz" in reply
