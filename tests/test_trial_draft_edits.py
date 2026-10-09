"""A draft with a chosen class must not lose it to facts that didn't change.

The extractor reads the whole conversation, so facts given earlier come back
on every turn. Merging them again is a no-op; only a real change — another
day/time, or intake that makes the class ineligible — replaces the class.
"""
from datetime import date, time

import pytest

from handlers import llm_trial_flow
from handlers.llm_trial_flow import LlmTrialFlowHandler

pytestmark = pytest.mark.no_db

_WED = date(2026, 9, 30)
_FRI = date(2026, 10, 2)
_SLOTS = [
    {"group_id": 7, "group_name": "Ерлан", "date": _WED, "time_start": time(18, 0),
     "time_end": time(19, 0), "level": ["Beginner"]},
    {"group_id": 8, "group_name": "Ерлан", "date": _FRI, "time_start": time(18, 0),
     "time_end": time(19, 0), "level": ["Beginner"]},
]
_KNOWN = {
    "child_name": "Алихан",
    "child_birth_year": 2016,
    "experience": "Beginner",
    "school_shift": "morning",
}
_DRAFT = {
    "id": 11, **_KNOWN, "preferred_date": _WED, "preferred_time_start": time(18, 0),
    "trial_day": _WED, "start_time": time(18, 0), "end_time": time(19, 0), "group_id": 7,
}


@pytest.fixture
def flow(monkeypatch):
    calls = {"updates": [], "assigned": None}
    state = {"draft": dict(_DRAFT)}
    monkeypatch.setattr(llm_trial_flow, "today_almaty", lambda: date(2026, 9, 28))
    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_existing_trial_draft",
                        lambda phone, bot: dict(state["draft"]))
    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_groups_info",
                        lambda bot_name: [{"group_name": "Ерлан", "trainer": "Ерлан"}])

    def eligible(bot_name, year, shift, experience=None, **kw):
        return _SLOTS if shift == "morning" else []

    def fake_update(bot_name, trial_id, fields):
        calls["updates"].append(fields)
        state["draft"] = {**state["draft"], **fields}
        return {"ok": True, "data": {"trial": dict(state["draft"])}}

    def fake_assign(bot_name, trial_id, slot):
        calls["assigned"] = slot
        state["draft"] = {**state["draft"], "trial_day": slot["date"], "start_time": slot["time_start"],
                          "end_time": slot["time_end"], "group_id": slot["group_id"]}
        return {"ok": True, "data": {"trial": dict(state["draft"])}}

    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_eligible_trial_slots", eligible)
    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_fallback_trial_slots", lambda *a, **k: [])
    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_birth_year_trial_slots", lambda *a, **k: [])
    monkeypatch.setattr(llm_trial_flow.trial_service, "update_intake", fake_update)
    monkeypatch.setattr(llm_trial_flow.trial_service, "assign_slot", fake_assign)

    def run(text, extracted):
        monkeypatch.setattr(llm_trial_flow, "extract_trial_details", lambda h, t: dict(extracted))
        return LlmTrialFlowHandler().handle("chat-1", "7700", "dopsy_boxing", text, [], "ru")

    return run, calls, state


def test_repeating_known_facts_keeps_the_chosen_class(flow):
    run, calls, state = flow

    reply = run("хорошо, ждём", {**_KNOWN, "preferred_date": "2026-09-30", "preferred_time_start": "18:00"})

    assert state["draft"]["group_id"] == 7
    assert calls["assigned"] is None
    assert all(update.get("group_id", 7) == 7 for update in calls["updates"])
    assert "Ср 30.09.2026" in reply and "Всё верно?" in reply


def test_another_day_replaces_the_chosen_class(flow):
    run, calls, state = flow

    reply = run("давайте лучше в пятницу", {**_KNOWN, "preferred_date": "2026-10-02"})

    assert calls["updates"][-1]["group_id"] is None
    assert calls["assigned"]["group_id"] == 8
    assert "Пт 02.10.2026" in reply


def test_shift_change_that_rules_the_class_out_clears_it(flow):
    run, calls, state = flow

    reply = run("ой, он учится во вторую смену", {**_KNOWN, "school_shift": "afternoon"})

    assert calls["updates"][0]["school_shift"] == "afternoon"
    assert calls["assigned"] is None
    assert "позвоните нашему администратору" in reply


def test_name_change_keeps_the_class_and_shows_the_new_name(flow):
    run, calls, state = flow

    reply = run("его зовут Али, а не Алихан", {**_KNOWN, "child_name": "Али"})

    assert calls["updates"][0]["child_name"] == "Али"
    assert state["draft"]["group_id"] == 7
    assert "Имя ребенка: Али" in reply


def test_trainer_name_does_not_replace_the_childs_name(flow):
    run, calls, state = flow

    run("1 к Ерлану", {**_KNOWN, "child_name": "Ерлан"})

    assert calls["updates"][0]["child_name"] == "Алихан"
