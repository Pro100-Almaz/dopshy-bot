"""Children's intake: the opening, the question order, bare ages and shifts.

The boxing bot opens a new signup with "the trial is free" and asks the age
first. A bare number in reply to the age question is the age. Shift answers
in words («бірінші ауысым», «түстен кейін») and kindergarten are understood,
so the shift is never asked again.
"""
from datetime import date, time

import pytest

from handlers import edit_trial, llm_trial_flow
from handlers.llm_trial_flow import LlmTrialFlowHandler, _normalize_manual_value, shift_from_text

pytestmark = pytest.mark.no_db

_TODAY = date(2026, 10, 17)
_CHILD_GROUPS = [{"group_id": 7, "birth_years": list(range(2010, 2021))}]
_CHILD_SLOTS = [
    {"group_id": 7, "group_name": "Box Timur", "audience": "child", "date": date(2026, 10, 20),
     "time_start": time(17, 0), "time_end": time(18, 0), "level": ["Beginner"]},
]
_EMPTY = {
    "child_name": None, "child_birth_year": None, "experience": None, "school_shift": None,
    "preferred_date": None, "preferred_time_start": None,
}


@pytest.mark.parametrize("text, expected", [
    ("Бірінші ауысымда оқимын", "morning"),
    ("1-ші ауысым", "morning"),
    ("первая смена", "morning"),
    ("учится утром", "morning"),
    ("Екінші ауысымда оқимын", "afternoon"),
    ("Түстен кейін оқимын", "afternoon"),
    ("вторая смена", "afternoon"),
    ("после обеда учится", "afternoon"),
    ("2 смена", "afternoon"),
    ("Балабақшаға барады", "kindergarten"),
    ("бала бақшаға барады, түстен кейін үйде", "kindergarten"),
    ("ходит в садик", "kindergarten"),
    ("в детский сад ходит", "kindergarten"),
    ("білмеймін", None),
])
def test_shift_from_text(text, expected):
    assert shift_from_text(text) == expected


@pytest.mark.parametrize("text, expected", [("1", "morning"), ("2", "afternoon"), ("3", "kindergarten")])
def test_shift_list_choice(text, expected):
    assert _normalize_manual_value("school_shift", text) == expected


@pytest.mark.parametrize("text, expected", [("8", 2018), ("8 жас", 2018), ("2016", 2016), ("ммм", None)])
def test_bare_age_while_waiting_for_age(monkeypatch, text, expected):
    monkeypatch.setattr(llm_trial_flow, "today_almaty", lambda: _TODAY)
    assert _normalize_manual_value("child_birth_year", text) == expected


def test_edit_patch_understands_shift_words():
    assert edit_trial._normalize_edit_patch({}, {"school_shift": "балабақшаға барады"}) == {
        "school_shift": "kindergarten"}
    assert edit_trial._normalize_edit_patch({}, {"school_shift": "екінші ауысым"}) == {
        "school_shift": "afternoon"}


@pytest.fixture
def flow(monkeypatch):
    state = {"draft": None}
    monkeypatch.setattr(llm_trial_flow, "today_almaty", lambda: _TODAY)
    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_existing_trial_draft",
                        lambda phone, bot: dict(state["draft"]) if state["draft"] else None)
    # No adult group: the children's questions start straight away.
    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_groups_info",
                        lambda bot_name, audience=None: _CHILD_GROUPS if audience == "child" else [])
    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_eligible_trial_slots", lambda *a, **k: _CHILD_SLOTS)
    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_fallback_trial_slots", lambda *a, **k: [])

    def fake_create(bot_name, chat_id, phone, lang):
        state["draft"] = {"id": 5, "audience": None, "group_id": None}
        return {"ok": True, "data": {"trial": dict(state["draft"])}}

    def fake_update(bot_name, trial_id, fields):
        state["draft"] = {**state["draft"], **fields}
        return {"ok": True, "data": {"trial": dict(state["draft"])}}

    monkeypatch.setattr(llm_trial_flow.trial_service, "create_or_get_draft", fake_create)
    monkeypatch.setattr(llm_trial_flow.trial_service, "update_intake", fake_update)

    def run(text, extracted=None, bot="dopsy_boxing", lang="kk"):
        monkeypatch.setattr(llm_trial_flow, "extract_trial_details",
                            lambda h, t: {**_EMPTY, **(extracted or {})})
        return LlmTrialFlowHandler().handle("chat-1", "7700", bot, text, [], lang)

    return run, state


def test_boxing_opens_with_free_trial_and_asks_the_age(flow):
    run, _ = flow

    reply = run("Сынақ жаттығуы туралы білгім келеді")

    assert reply == "Сынақ сабағы тегін 🥊\n\nБалаңыз неше жаста? Туған жылын да жазуға болады, мысалы: 2016."
    assert "рақмет" not in reply.lower()


def test_free_trial_intro_only_on_the_first_reply(flow):
    run, _ = flow
    run("Сынақ сабағына жазылғым келеді")

    reply = run("8")

    assert "тегін" not in reply
    assert reply == llm_trial_flow.BOXING_T["ask_name"]["kk"]


def test_age_and_class_in_one_message_asks_the_name_next(flow):
    run, state = flow

    reply = run("Балам 8 жаста, үшінші сыныпта оқиды")

    assert state["draft"]["child_birth_year"] == 2018
    assert reply.endswith(llm_trial_flow.BOXING_T["ask_name"]["kk"])


def test_bare_age_is_not_asked_again(flow):
    run, state = flow
    run("Сынақ сабағына жазылғым келеді")

    run("8")

    assert state["draft"]["child_birth_year"] == 2018


def test_bare_age_out_of_range_says_why(flow):
    run, _ = flow
    run("Сынақ сабағына жазылғым келеді")

    reply = run("3")

    assert "6 мен 16 жас" in reply


@pytest.mark.parametrize("answer, expected", [
    ("Екінші ауысымда оқиды", "afternoon"),
    ("Түстен кейін оқиды", "afternoon"),
    ("Бірінші ауысым", "morning"),
    ("Балабақшаға барады", "kindergarten"),
])
def test_shift_in_words_is_taken_and_not_asked_again(flow, answer, expected):
    run, state = flow
    run("Сынақ сабағына жазылғым келеді",
        extracted={"child_birth_year": 2018, "child_name": "Айдос", "experience": "Beginner"})

    reply = run(answer)

    assert state["draft"]["school_shift"] == expected
    assert "ауысымда оқиды" not in reply


def test_kindergarten_shows_in_the_confirmation(flow):
    run, state = flow
    state["draft"] = {"id": 5, "audience": "child", "group_id": 7, "child_name": "Айдос",
                      "child_birth_year": 2020, "experience": "Beginner", "school_shift": "kindergarten",
                      "trial_day": date(2026, 10, 20), "start_time": time(17, 0), "end_time": time(18, 0)}

    assert "Ауысым: балабақша" in llm_trial_flow._confirmation(state["draft"], "kk")


def test_football_keeps_its_order_and_has_no_intro(flow):
    run, _ = flow

    reply = run("Запишите сына на пробное", bot="dopsy_fs_school", lang="ru")

    assert reply == llm_trial_flow.T["ask_name"]["ru"]
