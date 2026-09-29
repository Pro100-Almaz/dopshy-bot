"""trial_select_slot must not drop the chosen day on a side question.

The extractor reads the whole conversation, so facts given earlier come back
on every turn; only a real change to a slot-affecting field (or an explicit
request for another day) may send the client back to day selection.
"""
from datetime import date

import pytest

from handlers.llm_trial_flow import LlmTrialFlowHandler

pytestmark = pytest.mark.no_db

_DRAFT = {
    "id": 11,
    "child_name": "Алихан",
    "child_birth_year": 2016,
    "experience": "Beginner",
    "school_shift": "morning",
    "preferred_date": None,
    "trial_day": None,
}
_SESSION = {
    "state": "trial_select_slot",
    "params": {
        "trial_id": 11,
        "lang": "ru",
        "chosen_date": "2026-09-30",
        "slots": [{
            "group_id": 7, "date": "2026-09-30",
            "time_start": "18:00", "time_end": "19:00", "level": ["Beginner"],
        }],
    },
}


@pytest.fixture
def flow(monkeypatch):
    calls = {"updates": [], "continued": 0, "fallback": []}
    monkeypatch.setattr("handlers.llm_trial_flow.academy_repo.get_trial", lambda trial_id: dict(_DRAFT))

    def fake_update(bot_name, trial_id, fields):
        calls["updates"].append(fields)
        return {"ok": True, "data": {"trial": {**_DRAFT, **fields}}}

    def fake_continue(self, chat_id, bot_name, draft, lang, signup_actor=None):
        calls["continued"] += 1
        return "choose day again"

    def fake_fallback(bot_name, lang, user_text, reminder, **kwargs):
        calls["fallback"].append(reminder)
        return f"answer\n\n{reminder}"

    monkeypatch.setattr("handlers.llm_trial_flow.trial_service.update_intake", fake_update)
    monkeypatch.setattr(LlmTrialFlowHandler, "_continue_from_draft", fake_continue)
    monkeypatch.setattr("handlers.llm_trial_flow._reasoned_fallback", fake_fallback)

    def run(text, extracted):
        monkeypatch.setattr(
            "handlers.llm_trial_flow._extract_user_data", lambda *a, **k: dict(extracted),
        )
        return LlmTrialFlowHandler().handle_session_turn(
            "chat-1", "7700", "dopsy_boxing", text, [], _SESSION,
        )

    return run, calls


_KNOWN = {
    "child_name": "Алихан",
    "child_birth_year": 2016,
    "experience": "Beginner",
    "school_shift": "morning",
}


def test_side_question_repeating_known_facts_keeps_the_chosen_day(flow):
    run, calls = flow

    reply = run("а какое расписание у тренера Ерлана?", _KNOWN)

    assert calls["updates"] == []
    assert calls["continued"] == 0
    assert len(calls["fallback"]) == 1
    assert "18:00" in reply


def test_question_naming_another_day_does_not_reset(flow):
    run, calls = flow

    run("а что в пятницу у Ерлана?", {**_KNOWN, "preferred_date": date(2026, 10, 2)})

    assert calls["updates"] == []
    assert calls["continued"] == 0
    assert len(calls["fallback"]) == 1


def test_explicit_request_for_another_day_returns_to_day_selection(flow):
    run, calls = flow

    reply = run("давайте лучше в пятницу", {**_KNOWN, "preferred_date": date(2026, 10, 2)})

    assert reply == "choose day again"
    assert calls["updates"][0]["trial_day"] is None
    assert calls["updates"][0]["preferred_date"] == date(2026, 10, 2)


def test_real_shift_change_resets_the_slot(flow):
    run, calls = flow

    reply = run("ой, он учится во вторую смену", {**_KNOWN, "school_shift": "afternoon"})

    assert reply == "choose day again"
    assert calls["updates"][0]["school_shift"] == "afternoon"
    assert calls["updates"][0]["group_id"] is None


def test_name_change_updates_name_and_keeps_the_group_list(flow):
    run, calls = flow

    reply = run("его зовут Али, а не Алихан", {**_KNOWN, "child_name": "Али"})

    assert calls["continued"] == 0
    assert calls["updates"] == [{"child_name": "Али", "language": "ru"}]
    assert reply.startswith("Спасибо, данные обновлены!")
    assert "18:00" in reply
