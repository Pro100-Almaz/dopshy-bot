"""Mid-signup routing: fast path for plain answers, router for the rest."""
import json
from types import SimpleNamespace

import pytest

from chat import llm
from handlers import message_handler
from handlers.llm_trial_flow import LlmTrialFlowHandler, _is_bare_yes_no, _offered_weekday

pytestmark = pytest.mark.no_db

# 2026-09-28 is a Monday, 2026-09-30 a Wednesday.
_DAY_SESSION = {
    "state": "trial_select_day",
    "params": {"trial_id": 11, "lang": "ru", "dates": ["2026-09-28", "2026-09-30"], "slots": []},
}
_SLOT_SESSION = {
    "state": "trial_select_slot",
    "params": {
        "trial_id": 11, "lang": "ru", "chosen_date": "2026-09-30",
        "slots": [{"group_id": 7, "group_name": "Ерлан", "date": "2026-09-30",
                   "time_start": "18:00", "time_end": "19:00"}],
    },
}
_CONFIRM_SESSION = {"state": "trial_confirm", "params": {"trial_id": 11, "lang": "ru"}}


@pytest.fixture
def turns(monkeypatch):
    seen = []
    monkeypatch.setattr(
        LlmTrialFlowHandler, "handle_session_turn",
        lambda self, chat_id, phone, bot, text, history, session: seen.append(text) or "flow",
    )
    return seen


def test_bare_yes_no_only_matches_the_whole_message():
    assert _is_bare_yes_no("Да!")
    assert _is_bare_yes_no("нет")
    assert not _is_bare_yes_no("да, а сколько стоит?")


def test_offered_weekday_requires_a_bare_weekday_on_offer():
    dates = _DAY_SESSION["params"]["dates"]
    assert _offered_weekday("в среду", dates)
    assert not _offered_weekday("пятница", dates)
    assert not _offered_weekday("а что в среду у Ерлана", dates)


@pytest.mark.parametrize("session, text", [
    (_DAY_SESSION, "2"),
    (_DAY_SESSION, "среда"),
    (_SLOT_SESSION, "1"),
    (_CONFIRM_SESSION, "да"),
    ({"state": "trial_intake", "params": {"lang": "ru", "waiting_for": "child_birth_year"}}, "2016"),
    ({"state": "trial_intake", "params": {"lang": "ru", "waiting_for": "school_shift"}}, "утренняя"),
])
def test_plain_answers_take_the_fast_path(turns, session, text):
    assert LlmTrialFlowHandler().try_fast_path("c", "p", "dopsy_boxing", text, [], session) == "flow"


@pytest.mark.parametrize("session, text", [
    (_SLOT_SESSION, "какое расписание у тренера Асана?"),
    (_DAY_SESSION, "а что в пятницу?"),
    (_CONFIRM_SESSION, "да, а сколько стоит?"),
    ({"state": "trial_intake", "params": {"lang": "ru", "waiting_for": "child_birth_year"}},
     "сколько стоит занятие?"),
])
def test_other_messages_go_to_the_router(turns, session, text):
    assert LlmTrialFlowHandler().try_fast_path("c", "p", "dopsy_boxing", text, [], session) is None
    assert turns == []


def test_pending_prompt_reshows_the_group_list():
    prompt = LlmTrialFlowHandler().pending_prompt("dopsy_boxing", _SLOT_SESSION)
    assert "Вот группы на Ср 30.09.2026" in prompt
    assert "1. Ерлан 18:00–19:00" in prompt


def test_router_gets_the_pending_step_and_can_fail_to_none(monkeypatch):
    seen = {}

    def failing_create(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("timeout")

    monkeypatch.setattr(llm._client.chat.completions, "create", failing_create)

    assert llm.route_trial_message([], "2", pending="Выберите группу", fallback=None) == (None, "ru")
    assert "Выберите группу" in seen["messages"][0]["content"]


def test_router_passes_intent_through(monkeypatch):
    completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[
        SimpleNamespace(function=SimpleNamespace(
            arguments=json.dumps({"type": "question_schedule", "lang": "ru"})))
    ]))])
    monkeypatch.setattr(llm._client.chat.completions, "create", lambda **kw: completion)

    assert llm.route_trial_message([], "расписание Асана?", pending="x", fallback=None) == (
        "question_schedule", "ru")


def _dispatch(intent, text="x"):
    return message_handler._dispatch_in_trial_session(
        intent, _SLOT_SESSION, "PENDING", "c", "p", "dopsy_boxing", text, [], "ru",
    )


@pytest.mark.parametrize("intent", [None, "trial_continue", "trial_new", "trial_edit"])
def test_signup_intents_and_router_failure_stay_in_the_flow(turns, intent):
    assert _dispatch(intent) == "flow"


def test_status_answer_reshows_the_pending_step(monkeypatch, turns):
    monkeypatch.setattr(message_handler, "handle_trial_status_request", lambda *a: "STATUS")

    assert _dispatch("trial_status") == "STATUS\n\nPENDING"
    assert turns == []


@pytest.mark.parametrize("intent, text", [
    ("other", "x"),
    ("question_price", "сколько стоит?"),
    ("question_payment", "как оплатить?"),
    ("question_schedule", "что в пятницу?"),
    ("question_schedule", "расписание тренера Асана"),
])
def test_info_questions_go_to_rag(turns, intent, text):
    assert _dispatch(intent, text) is None
    assert turns == []
