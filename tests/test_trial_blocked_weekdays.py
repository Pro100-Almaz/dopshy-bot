"""The football school offers no trials on Friday or Saturday, although groups
train then: those classes stay in the schedule but never become trial slots.
Boxing has no such rule.
"""
from datetime import time

import pytest

from integrations import trial

pytestmark = pytest.mark.no_db

_GROUPS = [
    {"group_id": day + 1, "group_name": f"G{day}", "training_day": day,
     "time_start": time(15, 0), "time_end": time(16, 15),
     "birth_years": [2016], "level": ["Beginner"], "max_cap": 25, "curr_cap": 0}
    for day in range(7)
]


@pytest.fixture(autouse=True)
def _groups(monkeypatch):
    monkeypatch.setattr(trial.academy_repo, "get_groups_info",
                        lambda bot_name, audience=None: [dict(g) for g in _GROUPS])


def _days(slots):
    return {s["training_day"] if "training_day" in s else s["date"].weekday() for s in slots}


@pytest.mark.parametrize("slots_of", [
    lambda bot: trial.get_trial_daytime(bot, None),
    lambda bot: trial.get_eligible_trial_slots(bot, 2016, "kindergarten", experience="Beginner"),
    lambda bot: trial.get_birth_year_trial_slots(bot, 2016),
    lambda bot: trial.get_adult_trial_slots(bot),
])
def test_football_trials_skip_friday_and_saturday(slots_of):
    assert _days(slots_of("dopsy_fs_school")) == {0, 1, 2, 3, 6}
    assert _days(slots_of("dopsy_boxing")) == set(range(7))


def test_schedule_question_still_lists_friday_and_saturday():
    classes = trial.list_classes_by_weekday("dopsy_fs_school", [4, 5])
    assert {c["training_day"] for c in classes} == {4, 5}
