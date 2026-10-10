"""Picking a class, editing and confirming — all through LlmTrialFlowHandler.handle.

The flow keeps no session: every message is extracted, merged into the draft
and the draft is evaluated again.
"""
from datetime import date, time

import pytest

from handlers import edit_trial, llm_trial_flow
from handlers.llm_trial_flow import LlmTrialFlowHandler

pytestmark = pytest.mark.no_db

_SLOT = {
    "group_id": 7, "group_name": "U15", "training_day": 0, "date": date(2026, 8, 24),
    "time_start": time(18, 0), "time_end": time(19, 30), "level": ["Beginner"],
}
_INTAKE = {
    "id": 11, "child_name": "Ерсултан", "child_birth_year": 2011,
    "experience": "Beginner", "school_shift": "afternoon",
}
_CHOSEN = {
    "trial_day": date(2026, 8, 24), "start_time": time(18, 0), "end_time": time(19, 30), "group_id": 7,
}


@pytest.fixture
def flow(monkeypatch):
    state = {"draft": None, "updates": [], "assigned": None, "confirmed": False}
    monkeypatch.setattr(llm_trial_flow, "today_almaty", lambda: date(2026, 8, 20))
    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_existing_trial_draft",
                        lambda phone, bot: dict(state["draft"]))
    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_groups_info", lambda bot_name: [])
    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_eligible_trial_slots", lambda *a, **k: [_SLOT])

    def fake_update(bot_name, trial_id, fields):
        state["updates"].append(fields)
        state["draft"] = {**state["draft"], **fields}
        return {"ok": True, "data": {"trial": dict(state["draft"])}}

    def fake_assign(bot_name, trial_id, slot):
        state["assigned"] = slot
        state["draft"] = {**state["draft"], "trial_day": slot["date"], "start_time": slot["time_start"],
                          "end_time": slot["time_end"], "group_id": slot["group_id"]}
        return {"ok": True, "data": {"trial": dict(state["draft"])}}

    def fake_confirm(bot_name, chat_id, trial_id):
        state["confirmed"] = True
        return {"ok": True, "data": {"trial": dict(state["draft"])}}

    monkeypatch.setattr(llm_trial_flow.trial_service, "update_intake", fake_update)
    monkeypatch.setattr(llm_trial_flow.trial_service, "assign_slot", fake_assign)
    monkeypatch.setattr(llm_trial_flow.trial_service, "confirm_trial", fake_confirm)

    def run(text, draft, extracted=None):
        state["draft"] = dict(draft)
        monkeypatch.setattr(llm_trial_flow, "extract_trial_details", lambda h, t: dict(extracted or {}))
        return LlmTrialFlowHandler().handle("chat-1", "7700", "dopsy_fs_school", text, [], "ru")

    return run, state


def test_choice_from_the_group_list_assigns_the_class(flow):
    run, state = flow

    reply = run("1 18:00", _INTAKE, {"preferred_date": "2026-08-24", "preferred_time_start": "18:00"})

    assert state["assigned"]["group_id"] == 7
    assert "📋 Детали записи" in reply and "18:00–19:30" in reply
    assert "Ответьте *да* или *нет*" in reply


def test_a_bare_list_number_is_not_read_as_a_level(flow):
    run, state = flow

    run("2", _INTAKE, {"preferred_date": "2026-08-24"})

    assert state["updates"][0]["experience"] == "Beginner"


def test_edit_with_a_chosen_class_updates_the_draft_and_reasks_confirmation(flow):
    run, state = flow

    reply = run("поменяйте смену на утреннюю", {**_INTAKE, **_CHOSEN, "school_shift": "afternoon"},
                {"school_shift": "morning"})

    assert state["updates"][0]["school_shift"] == "morning"
    assert "🏫 Смена: утренняя" in reply
    assert "Ответьте *да* или *нет*" in reply
    assert not state["confirmed"]


def test_yes_confirms_the_chosen_class(flow, monkeypatch):
    run, state = flow
    monkeypatch.setattr(llm_trial_flow.trial_logic, "is_trial_slot_eligible", lambda bot, draft: True)

    reply = run("да", {**_INTAKE, **_CHOSEN})

    assert state["confirmed"]
    assert reply.startswith("Вы записаны на пробный урок")


def test_yes_clears_an_ineligible_class_and_reselects(flow, monkeypatch):
    run, state = flow
    monkeypatch.setattr(llm_trial_flow.trial_logic, "is_trial_slot_eligible", lambda bot, draft: False)
    monkeypatch.setattr(LlmTrialFlowHandler, "_evaluate_and_respond",
                        lambda self, chat_id, bot_name, draft, lang, signup_actor=None: "choose new group")

    reply = run("да", {**_INTAKE, **_CHOSEN})

    assert not state["confirmed"]
    assert state["updates"][0]["group_id"] is None
    assert state["updates"][0]["trial_day"] is None
    assert state["updates"][0]["preferred_date"] is None
    assert "выберите подходящий вариант заново" in reply
    assert "choose new group" in reply


def test_confirmed_edit_uses_replacement_draft_flow(monkeypatch):
    monkeypatch.setattr(
        edit_trial,
        "extract_trial_details",
        lambda history, user_text: {"school_shift": "morning"},
    )
    monkeypatch.setattr(
        edit_trial,
        "_active_trials",
        lambda bot_name, sender_phone: [{"id": 11, "state": "confirmed"}],
    )
    monkeypatch.setattr(
        edit_trial.trial_service,
        "replace_confirmed_trial_with_draft",
        lambda bot_name, chat_id, trial_id, patch, lang: {
            "ok": True,
            "data": {"trial": {"id": 22, "child_birth_year": 2011, "experience": "Beginner", "school_shift": "morning", "child_name": "Ерсултан"}},
        },
    )
    monkeypatch.setattr(
        LlmTrialFlowHandler,
        "_evaluate_and_respond",
        lambda self, chat_id, bot_name, draft, lang, signup_actor=None: "choose group",
    )

    reply = edit_trial.handle_edit_request(
        "chat-1",
        "7700",
        {},
        "dopsy_fs_school",
        "поменяйте смену на утреннюю",
        [],
        "ru",
    )

    assert "Подтвержденную запись нельзя изменить напрямую" in reply
    assert "создал новый черновик" in reply
    assert "choose group" in reply
