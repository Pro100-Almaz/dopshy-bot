"""The boxing bot's adult group (18+).

Before any signup question the client picks the children's or the adult group
— unless the message already says which («мен 25 жастамын», «ересектер
тобы»). The adult signup asks no age, level or school shift: schedule first,
then the name, then the confirmation.
"""
from datetime import date, time

import pytest

from handlers import llm_trial_flow
from handlers.llm_trial_flow import LlmTrialFlowHandler, _audience_choice, _detect_audience

pytestmark = pytest.mark.no_db

_TODAY = date(2026, 10, 17)
_TUE, _THU, _SAT = date(2026, 10, 20), date(2026, 10, 22), date(2026, 10, 24)
_ADULT_SLOTS = [
    {"group_id": 30, "group_name": "Box Adult", "audience": "adult", "date": d,
     "time_start": start, "time_end": end}
    for d, start, end in (
        (_TUE, time(19, 15), time(20, 30)),
        (_THU, time(19, 15), time(20, 30)),
        (_SAT, time(12, 0), time(13, 15)),
    )
]
_CHILD_SLOTS = [
    {"group_id": 7, "group_name": "Box Timur", "audience": "child", "date": _TUE,
     "time_start": time(17, 0), "time_end": time(18, 0), "level": ["Beginner"]},
]
_CHILD_GROUPS = [{"group_id": 7, "birth_years": list(range(2009, 2023))}]
_EMPTY = {
    "child_name": None, "child_birth_year": None, "experience": None, "school_shift": None,
    "preferred_date": None, "preferred_time_start": None,
}


@pytest.mark.parametrize("text, expected", [
    ("Мен 25 жастамын", "adult"),
    ("30 жастамын, боксқа жазылғым келеді", "adult"),
    ("мне 40 лет, хочу записаться", "adult"),
    ("Ересектерге арналған бокс бар ма?", "adult"),
    ("есть бокс для взрослых?", "adult"),
    ("18 жастан үлкен ересектер тобы", "adult"),
    ("10-18 жас балалар тобы", "child"),
    ("балам 10 жаста", "child"),
    ("мне 35 лет, хочу записать сына", "child"),
    ("18 жасқа дейінгі топ", None),
    ("сынақ сабағына жазылғым келеді", None),
])
def test_detect_audience(text, expected):
    assert _detect_audience(text, today=_TODAY) == expected


def test_detect_audience_by_birth_year():
    assert _detect_audience("", birth_year=2000, today=_TODAY) == "adult"
    assert _detect_audience("", birth_year=2008, today=_TODAY) == "adult"
    assert _detect_audience("", birth_year=2015, today=_TODAY) == "child"


@pytest.mark.parametrize("text, expected", [("1", "child"), ("2", "adult"), ("2.", "adult"), ("3", None)])
def test_audience_choice(text, expected):
    assert _audience_choice(text) == expected


@pytest.fixture
def flow(monkeypatch):
    state = {"draft": None, "confirmed": False}
    adult_groups = {"exist": True}
    monkeypatch.setattr(llm_trial_flow, "today_almaty", lambda: _TODAY)
    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_existing_trial_draft",
                        lambda phone, bot: dict(state["draft"]) if state["draft"] else None)
    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_groups_info",
                        lambda bot_name, audience=None:
                        _ADULT_SLOTS if audience == "adult" and adult_groups["exist"]
                        else _CHILD_GROUPS if audience == "child" else [])
    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_adult_trial_slots",
                        lambda bot_name: _ADULT_SLOTS if adult_groups["exist"] else [])
    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_eligible_trial_slots", lambda *a, **k: _CHILD_SLOTS)
    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_fallback_trial_slots", lambda *a, **k: [])
    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_birth_year_trial_slots", lambda *a, **k: [])
    monkeypatch.setattr(llm_trial_flow.trial_logic, "is_trial_slot_eligible", lambda bot, trial: True)

    def fake_create(bot_name, chat_id, phone, lang):
        state["draft"] = {"id": 5, "audience": None, "group_id": None}
        return {"ok": True, "data": {"trial": dict(state["draft"])}}

    def fake_update(bot_name, trial_id, fields):
        state["draft"] = {**state["draft"], **fields}
        return {"ok": True, "data": {"trial": dict(state["draft"])}}

    def fake_assign(bot_name, trial_id, slot):
        state["draft"] = {**state["draft"], "trial_day": slot["date"], "start_time": slot["time_start"],
                          "end_time": slot["time_end"], "group_id": slot["group_id"]}
        return {"ok": True, "data": {"trial": dict(state["draft"])}}

    def fake_confirm(bot_name, chat_id, trial_id):
        state["confirmed"] = True
        return {"ok": True, "data": {"trial": dict(state["draft"])}}

    monkeypatch.setattr(llm_trial_flow.trial_service, "create_or_get_draft", fake_create)
    monkeypatch.setattr(llm_trial_flow.trial_service, "update_intake", fake_update)
    monkeypatch.setattr(llm_trial_flow.trial_service, "assign_slot", fake_assign)
    monkeypatch.setattr(llm_trial_flow.trial_service, "confirm_trial", fake_confirm)

    def run(text, extracted=None, history=None, lang="kk", bot="dopsy_boxing"):
        monkeypatch.setattr(llm_trial_flow, "extract_trial_details",
                            lambda h, t: {**_EMPTY, **(extracted or {})})
        return LlmTrialFlowHandler().handle("chat-1", "7700", bot, text, history or [], lang)

    return run, state, adult_groups


def _assert_adult_schedule(reply):
    assert "Ересектер тобының жаттығулары" in reply
    assert "Сс 20.10.2026 — 19:15" in reply
    assert "Бс 22.10.2026 — 19:15" in reply
    assert "Сб 24.10.2026 — 12:00" in reply
    # The technical end time is never shown, nor any child question.
    assert "20:30" not in reply and "13:15" not in reply
    assert "туған жыл" not in reply and "ауысым" not in reply


def test_signup_without_a_group_asks_which_group(flow):
    run, state, _ = flow

    reply = run("сынақ сабағына жазылғым келеді")

    assert "Керек топты таңдаңыз" in reply
    # The children's range comes from the groups' birth years (2009-2022 here)
    # and stops below the adult group, which starts at 18.
    assert "1. 4-17 жас балалар тобы" in reply and "2. 18 жастан бастап ересектер тобы" in reply
    assert state["draft"]["audience"] is None


def test_picking_the_adult_group_shows_its_schedule_at_once(flow):
    run, state, _ = flow
    run("сынақ сабағына жазылғым келеді")

    reply = run("2")

    assert state["draft"]["audience"] == "adult"
    _assert_adult_schedule(reply)


def test_picking_the_children_group_starts_the_child_questions(flow):
    run, state, _ = flow
    run("сынақ сабағына жазылғым келеді")

    reply = run("1")

    assert state["draft"]["audience"] == "child"
    assert "Балаңыз неше жаста" in reply


def test_stated_adult_age_skips_the_choice(flow):
    run, state, _ = flow

    reply = run("Мен 25 жастамын, боксқа жазылғым келеді")

    assert state["draft"]["audience"] == "adult"
    _assert_adult_schedule(reply)


def test_adult_group_named_earlier_in_the_chat_counts(flow):
    run, state, _ = flow
    history = [{"role": "user", "content": "Ересектерге арналған бокс бар ма?"},
               {"role": "assistant", "content": "Иә, бар."}]

    reply = run("жазылғым келеді", history=history)

    assert state["draft"]["audience"] == "adult"
    _assert_adult_schedule(reply)


def test_adult_signup_saturday_then_name_then_confirm(flow):
    run, state, _ = flow
    run("ересектер тобына жазылғым келеді")

    reply = run("сенбі күні", {"preferred_date": "2026-10-24"})
    assert state["draft"]["group_id"] == 30 and state["draft"]["start_time"] == time(12, 0)
    assert "Есіміңіз кім" in reply

    reply = run("Айдос", {"child_name": "Айдос"})
    assert "Ересектер тобы" in reply and "Сб 24.10.2026" in reply and "12:00" in reply
    assert "Айдос" in reply and "13:15" not in reply

    reply = run("иә")
    assert state["confirmed"] is True
    assert "сынақ сабағына жазылдыңыз" in reply and "Ересектер тобы" in reply


def test_todays_weekday_after_its_class_means_next_week(flow):
    run, state, _ = flow
    run("ересектерге бокс бар ма, жазылғым келеді")

    # Today is Saturday 17.10 and its class is already over: «сенбі» resolves
    # to today, the next Saturday class is 24.10.
    reply = run("Сенбі күні келемін, атым Айдос",
                {"preferred_date": "2026-10-17", "preferred_time_start": "12:00", "child_name": "Айдос"})

    assert state["draft"]["trial_day"] == _SAT
    assert "жаттығуы жоқ" not in reply
    assert "Сб 24.10.2026" in reply and "Айдос" in reply


def test_schedule_that_does_not_fit_gets_no_invented_time(flow):
    run, state, _ = flow
    run("ересектер тобына жазылғым келеді")

    reply = run("бұл уақыттар маған келмейді")

    assert "басқа жаттығу уақыты қазір жоқ" in reply and "+7 700 555 2202" in reply
    _assert_adult_schedule(reply)


def test_yes_before_the_name_is_not_a_confirmation(flow):
    run, state, _ = flow
    run("ересектер тобына жазылғым келеді")
    run("сенбі күні", {"preferred_date": "2026-10-24"})

    run("иә")

    assert state["confirmed"] is False


def test_time_with_no_adult_class_lists_only_real_classes(flow):
    run, state, _ = flow
    run("ересектер тобына жазылғым келеді")

    reply = run("сағат 10-да келсем бола ма", {"preferred_time_start": "10:00"})

    assert "Бұл уақытта ересектер тобының жаттығуы жоқ" in reply
    _assert_adult_schedule(reply)
    assert state["draft"]["group_id"] is None


def test_evening_time_picks_between_tuesday_and_thursday(flow):
    run, state, _ = flow
    run("ересектер тобына жазылғым келеді")

    reply = run("19:15 ыңғайлы", {"preferred_time_start": "19:15"})

    assert "Сс 20.10.2026 — 19:15" in reply and "Бс 22.10.2026 — 19:15" in reply
    assert "12:00" not in reply


def test_adult_birth_year_in_child_flow_switches_to_adult(flow):
    run, state, _ = flow
    run("балама сынақ сабағы керек")
    run("Айдос", {"child_name": "Айдос"})
    assert state["draft"]["audience"] == "child"

    reply = run("2001", {"child_birth_year": 2001})

    assert state["draft"]["audience"] == "adult"
    assert state["draft"]["child_birth_year"] is None
    assert "сәйкес емес" not in reply and "келмейді" not in reply
    _assert_adult_schedule(reply)


def test_russian_adult_flow(flow):
    run, state, _ = flow

    reply = run("Мне 30 лет, хочу на пробное", lang="ru")

    assert "Тренировки взрослой группы" in reply
    assert "Вт 20.10.2026 — 19:15" in reply and "Сб 24.10.2026 — 12:00" in reply


def test_bot_without_adult_group_never_asks_which_group(flow):
    run, state, adult_groups = flow
    adult_groups["exist"] = False

    reply = run("сынақ сабағына жазылғым келеді", bot="dopsy_fs_school")

    assert "Керек топты таңдаңыз" not in reply
    assert state["draft"]["audience"] == "child"


def test_child_of_eighteen_goes_to_the_adult_group(flow):
    age = 18
    run, state, _ = flow
    run("балама сынақ сабағы керек")
    run("Айдос", {"child_name": "Айдос"})

    reply = run(f"балам {age} жаста")

    assert state["draft"]["audience"] == "adult"
    _assert_adult_schedule(reply)


def test_sixteen_year_old_is_a_child(flow):
    run, state, _ = flow
    run("сынақ сабағына жазылғым келеді")
    run("1")

    run("Маған 16 жас, атым Айдос", {"child_name": "Айдос"})

    assert state["draft"]["child_birth_year"] == 2010
    assert state["draft"]["audience"] == "child"


def test_parents_own_age_is_not_the_childs(flow):
    run, state, _ = flow

    reply = run("мне 35 лет, хочу записать сына", lang="ru")

    assert state["draft"]["audience"] == "child"
    assert state["draft"].get("child_birth_year") is None
    assert "нет группы" not in reply and "не подходит" not in reply


def test_full_adult_group_sends_to_admin_without_inventing_times(flow, monkeypatch):
    run, state, _ = flow
    run("ересектер тобына жазылғым келеді")
    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_adult_trial_slots", lambda bot_name: [])

    reply = run("сенбі күні", {"preferred_date": "2026-10-24"})

    assert "әкімші" in reply.lower()
    assert "12:00" not in reply and "19:15" not in reply
