"""LlmTrialFlowHandler._evaluate_and_respond — the draft alone decides the reply.

Counterpart of the arena's LlmBookingFlowHandler._evaluate_and_respond: no
session state; intake first, then the parent's date/time (preferred_*) narrows
the eligible classes, and a single match is assigned straight away.
"""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from handlers import llm_trial_flow
from handlers.llm_trial_flow import LlmTrialFlowHandler
from integrations import trial as trial_logic

pytestmark = pytest.mark.no_db

BOT = "dopsy_boxing"


def _slot(group_id, day, start, end, levels=("Beginner", "Intermediate", "Advanced")):
    return {
        "group_id": group_id,
        "group_name": "Box Timur",
        "training_day": day.weekday(),
        "date": day,
        "time_start": time.fromisoformat(start),
        "time_end": time.fromisoformat(end),
        "level": list(levels),
    }


SAT = date(2026, 10, 10)
MON = date(2026, 10, 12)
SLOTS = [
    _slot(1, SAT, "17:00", "18:00"),
    _slot(2, SAT, "18:00", "19:00"),
    _slot(3, SAT, "19:30", "20:30"),
    _slot(1, MON, "17:00", "18:00"),
]

INTAKE = {
    "id": 11,
    "child_name": "Тайсон",
    "child_birth_year": 2015,
    "experience": "Intermediate",
    "school_shift": "morning",
}


@pytest.fixture
def flow(monkeypatch):
    calls = {"assigned": None, "updated": None}
    monkeypatch.setattr(llm_trial_flow, "today_almaty", lambda: date(2026, 10, 9))
    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_eligible_trial_slots", lambda *a, **k: SLOTS)
    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_fallback_trial_slots", lambda *a, **k: [])
    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_birth_year_trial_slots", lambda *a, **k: [])

    def fake_assign(bot_name, trial_id, slot):
        calls["assigned"] = slot
        return {"ok": True, "data": {"trial": {
            **INTAKE,
            "trial_day": slot["date"],
            "start_time": slot["time_start"],
            "end_time": slot["time_end"],
            "group_id": slot["group_id"],
        }}}

    def fake_update(bot_name, trial_id, fields):
        calls["updated"] = fields
        return {"ok": True, "data": {"trial": {**INTAKE, **calls["draft"], **fields}}}

    monkeypatch.setattr(llm_trial_flow.trial_service, "assign_slot", fake_assign)
    monkeypatch.setattr(llm_trial_flow.trial_service, "update_intake", fake_update)

    def run(lang="kk", **draft):
        calls["draft"] = draft
        return LlmTrialFlowHandler()._evaluate_and_respond("chat-1", BOT, {**INTAKE, **draft}, lang)

    run.calls = calls
    return run


def test_missing_intake_field_is_asked_first(flow):
    reply = flow(school_shift=None)
    assert reply == llm_trial_flow.BOXING_T["ask_school_shift"]["kk"]
    assert flow.calls["assigned"] is None


def test_nothing_chosen_shows_the_days(flow):
    reply = flow()
    assert "Сынақ сабағына қай күн сізге ыңғайлы?" in reply
    assert "1. Сб 10.10.2026" in reply and "2. Дс 12.10.2026" in reply


def test_date_and_time_assign_the_class_and_confirm(flow):
    reply = flow(preferred_date=SAT, preferred_time_start=time(17, 0))
    assert flow.calls["assigned"]["group_id"] == 1
    assert flow.calls["assigned"]["date"] == SAT
    assert "Жазылым деректері" in reply and "17:00–18:00" in reply


def test_date_only_lists_that_days_classes(flow):
    reply = flow(preferred_date=SAT)
    assert "Сб 10.10.2026 күнгі топтар" in reply
    assert "1. Box Timur 17:00–18:00" in reply and "3. Box Timur 19:30–20:30" in reply
    assert flow.calls["assigned"] is None


def test_date_with_a_single_class_is_assigned(flow):
    flow(preferred_date=MON)
    assert flow.calls["assigned"]["date"] == MON


def test_time_only_on_several_days_lists_those_days(flow):
    reply = flow(preferred_time_start="17:00")
    assert "1. Сб 10.10.2026" in reply and "2. Дс 12.10.2026" in reply
    assert flow.calls["assigned"] is None


def test_time_only_on_one_day_is_assigned(flow):
    flow(preferred_time_start="18:00")
    assert flow.calls["assigned"]["group_id"] == 2


def test_unavailable_time_lists_the_day_with_a_note(flow):
    reply = flow(preferred_date=SAT, preferred_time_start="16:00")
    assert reply.startswith(llm_trial_flow.BOXING_T["preferred_unavailable"]["kk"])
    assert "Сб 10.10.2026 күнгі топтар" in reply
    assert flow.calls["assigned"] is None


def test_unavailable_date_lists_the_days_with_a_note(flow):
    reply = flow(preferred_date=date(2026, 10, 11))
    assert reply.startswith(llm_trial_flow.BOXING_T["preferred_unavailable"]["kk"])
    assert "1. Сб 10.10.2026" in reply
    assert flow.calls["assigned"] is None


def test_past_preferred_date_is_dropped_without_a_note(flow):
    reply = flow(preferred_date=date(2026, 10, 1))
    assert not reply.startswith(llm_trial_flow.BOXING_T["preferred_unavailable"]["kk"])
    assert "1. Сб 10.10.2026" in reply
    assert flow.calls["assigned"] is None


def test_chosen_class_that_still_fits_shows_the_confirmation(flow):
    reply = flow(preferred_date=SAT, preferred_time_start="17:00",
                 trial_day=SAT, start_time=time(17, 0), end_time=time(18, 0), group_id=1)
    assert "Жазылым деректері" in reply
    assert flow.calls["assigned"] is None and flow.calls["updated"] is None


def test_picking_another_option_replaces_the_chosen_class(flow):
    flow(preferred_date=SAT, preferred_time_start="18:00",
         trial_day=SAT, start_time=time(17, 0), end_time=time(18, 0), group_id=1)
    assert flow.calls["updated"]["group_id"] is None
    assert flow.calls["assigned"]["group_id"] == 2


def test_only_lower_level_groups_are_listed_with_a_note(flow, monkeypatch):
    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_eligible_trial_slots", lambda *a, **k: [])
    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_fallback_trial_slots",
                        lambda *a, **k: [_slot(4, SAT, "17:00", "18:00", levels=("Beginner",))])
    reply = flow(experience="Advanced")
    assert reply.startswith("Өкінішке қарай, қазір Жоғары деңгейіндегі топ жоқ.")
    assert "Сб 10.10.2026 күнгі топтар" in reply and "Бастапқы" in reply
    assert flow.calls["assigned"] is None


def test_no_groups_at_all_falls_back_to_the_admin(flow, monkeypatch):
    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_eligible_trial_slots", lambda *a, **k: [])
    assert flow() == llm_trial_flow.BOXING_T["no_groups_call_admin"]["kk"]


def test_pending_prompt_lists_options_without_assigning(flow):
    prompt = LlmTrialFlowHandler().pending_prompt(
        BOT, {**INTAKE, "preferred_date": SAT, "preferred_time_start": "17:00"}, "kk",
    )
    assert "Сб 10.10.2026 күнгі топтар" in prompt
    assert flow.calls["assigned"] is None and flow.calls["updated"] is None


def test_pending_prompt_does_not_repeat_the_unavailable_note(flow):
    prompt = LlmTrialFlowHandler().pending_prompt(BOT, {**INTAKE, "preferred_date": date(2026, 10, 11)}, "kk")
    assert llm_trial_flow.BOXING_T["preferred_unavailable"]["kk"] not in prompt
    assert prompt.startswith("Сынақ сабағына қай күн сізге ыңғайлы?")


def test_pending_prompt_follows_the_draft(flow):
    handler = LlmTrialFlowHandler()
    assert handler.pending_prompt(BOT, None, "kk") is None
    assert handler.pending_prompt(BOT, {**INTAKE, "child_name": None}, "kk") == \
        llm_trial_flow.BOXING_T["ask_name"]["kk"]
    chosen = {**INTAKE, "trial_day": SAT, "start_time": time(17, 0), "end_time": time(18, 0), "group_id": 1}
    assert "Жазылым деректері" in handler.pending_prompt(BOT, chosen, "kk")


@pytest.mark.parametrize(("now", "expected"), [
    (datetime(2026, 10, 9, 16, 0), date(2026, 10, 9)),   # Friday class at 17:00 still ahead
    (datetime(2026, 10, 9, 21, 16), date(2026, 10, 16)),  # already over → next Friday
])
def test_class_that_already_started_today_moves_to_next_week(monkeypatch, now, expected):
    tz = ZoneInfo("Asia/Almaty")
    monkeypatch.setattr("utils.now_almaty", lambda: now.replace(tzinfo=tz))
    assert trial_logic._next_class_date(4, time(17, 0)) == expected
