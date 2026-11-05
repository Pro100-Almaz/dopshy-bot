"""Regression tests for the flow getting stuck / misreading side messages.

Real production incidents this covers:
- "кстати можешь дать информацию по тренерам?" during trial_confirm was read
  as a "yes" (the word "да" is a substring of "дать") and silently confirmed
  a booking the user was trying to escape.
- "хочу отменить" / "хочу отменить все" during trial_confirm didn't match the
  cancel word list at all, so cancellation silently failed.
- "что ты несешь?" while asked for child_birth_year got echoed back verbatim
  in the "Год рождения {что ты несешь?} не подходит" message.
- "мои занятия" / "какие есть у меня пробные" during intake was treated as an
  attempted (invalid) value for whatever field was being asked, instead of
  answering the actual question.
"""

import pytest

from handlers import llm_trial_flow
from handlers.llm_trial_flow import BOXING_T, T, LlmTrialFlowHandler

pytestmark = pytest.mark.no_db


@pytest.fixture(autouse=True)
def _no_adult_group(monkeypatch):
    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_groups_info", lambda bot_name=None, audience=None: [])

_CHOSEN_DRAFT = {
    "id": 11, "trial_day": "2026-09-25", "start_time": "16:00", "end_time": "17:00", "group_id": 7,
    "child_name": "Ерсултан", "child_birth_year": 2016,
    "experience": "Intermediate", "school_shift": "morning",
}


def test_plain_no_cancels_the_signup(monkeypatch):
    _with_draft(monkeypatch, _CHOSEN_DRAFT)
    cancelled = []
    monkeypatch.setattr(llm_trial_flow.trial_service, "cancel_trial",
                        lambda bot_name, chat_id, trial_id, reason: cancelled.append((trial_id, reason)))

    LlmTrialFlowHandler().handle("chat-1", "7700", "dopsy_boxing", "жоқ", [], "kk", intent="confirm_no")

    assert cancelled == [(11, "user_declined_gated_trial")]


def test_fitting_slots_context_lists_only_this_childs_classes(monkeypatch):
    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_eligible_trial_slots", lambda *a, **k: [{
        "group_id": 7, "group_name": "Бокс 8-16", "date": "2026-10-23",
        "time_start": "10:00", "time_end": "11:00", "level": [],
    }])

    ctx = LlmTrialFlowHandler().fitting_slots_context("dopsy_boxing", _CHOSEN_DRAFT)

    assert "Пт 23.10.2026 10:00–11:00 — Бокс 8-16" in ctx
    assert LlmTrialFlowHandler().fitting_slots_context("dopsy_boxing", {"id": 11}) is None


def _with_draft(monkeypatch, draft, extracted=None):
    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_existing_trial_draft",
                        lambda phone, bot_name: dict(draft))
    monkeypatch.setattr(llm_trial_flow, "extract_trial_details",
                        lambda history, user_text, answering=None, wish=None: dict(extracted or {}))
    monkeypatch.setattr(llm_trial_flow.trial_service, "update_intake",
                        lambda bot_name, trial_id, fields: {"ok": True, "data": {"trial": {**draft, **fields}}})


def test_unrelated_question_with_a_chosen_class_does_not_autoconfirm(monkeypatch):
    _with_draft(monkeypatch, _CHOSEN_DRAFT)
    monkeypatch.setattr(llm_trial_flow.trial_service, "confirm_trial",
                        lambda *a: pytest.fail("must not confirm"))
    cancelled = []
    monkeypatch.setattr(llm_trial_flow.trial_service, "cancel_trial", lambda *a: cancelled.append(a))
    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_eligible_trial_slots", lambda *a, **k: [{
        "group_id": 7, "date": "2026-09-25", "time_start": "16:00", "time_end": "17:00", "level": [],
    }])

    reply = LlmTrialFlowHandler().handle(
        "chat-1", "7700", "dopsy_boxing", "кстати можешь дать информацию по тренерам?", [], "ru",
        intent="trial_continue",
    )

    # Neither a yes, a no nor a change: the summary is shown again.
    assert not cancelled and "Подтверждаете запись?" in reply


def test_cancel_phrase_with_a_chosen_class_actually_cancels(monkeypatch):
    _with_draft(monkeypatch, _CHOSEN_DRAFT)
    cancelled = {}
    monkeypatch.setattr(
        llm_trial_flow.trial_service, "cancel_trial",
        lambda bot_name, chat_id, trial_id, reason: cancelled.update(
            bot_name=bot_name, chat_id=chat_id, trial_id=trial_id, reason=reason
        ),
    )

    reply = LlmTrialFlowHandler().handle(
        "chat-1", "7700", "dopsy_boxing", "хочу отменить все", [], "ru", intent="confirm_no",
    )

    assert cancelled == {
        "bot_name": "dopsy_boxing", "chat_id": "chat-1",
        "trial_id": 11, "reason": "user_declined_gated_trial",
    }
    assert "отменена" in reply


def test_birth_year_gibberish_is_reasked_not_echoed_back(monkeypatch):
    _with_draft(monkeypatch, {"id": 11, "audience": "child", "child_name": "Ерсултан"})

    reply = LlmTrialFlowHandler().handle(
        "chat-1", "7700", "dopsy_fs_school", "что ты несешь?", [], "ru",
    )

    assert reply == T["ask_birth_year"]["ru"]
    assert "не подходит" not in reply


def test_opening_signup_sentence_is_not_taken_as_the_childs_name(monkeypatch):
    saved = {}
    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_existing_trial_draft", lambda phone, bot: None)
    monkeypatch.setattr(llm_trial_flow.trial_service, "create_or_get_draft",
                        lambda *a: {"ok": True, "data": {"trial": {"id": 11}}})
    monkeypatch.setattr(llm_trial_flow, "extract_trial_details",
                        lambda history, text, answering=None, wish=None: {"asks_about_trial_or_price": True, "audience": "child"})

    def fake_update(bot_name, trial_id, fields):
        saved.update(fields)
        return {"ok": True, "data": {"trial": {"id": 11, **fields}}}

    monkeypatch.setattr(llm_trial_flow.trial_service, "update_intake", fake_update)

    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_groups_info", lambda bot_name, audience=None: [])

    reply = LlmTrialFlowHandler().handle(
        "chat-1", "7700", "dopsy_boxing", "Балама сынақ сабағы керек", [], "kk",
    )

    assert not saved.get("child_name")
    # A new signup: "the trial is free", then the name.
    assert reply == f"{BOXING_T['trial_free_intro']['kk']}\n\n{BOXING_T['ask_name']['kk']}"


def test_sentence_on_return_to_a_draft_is_not_taken_as_the_name(monkeypatch):
    from datetime import datetime, timedelta, timezone

    # Not yet abandoned (120 minutes): the message answers the name question.
    stale = {"id": 11, "audience": "child", "updated_at": datetime.now(timezone.utc) - timedelta(minutes=60)}
    saved = {}
    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_existing_trial_draft", lambda phone, bot: dict(stale))
    monkeypatch.setattr(llm_trial_flow, "extract_trial_details", lambda history, text, answering=None, wish=None: {})

    def fake_update(bot_name, trial_id, fields):
        saved.update(fields)
        return {"ok": True, "data": {"trial": {**stale, **fields}}}

    monkeypatch.setattr(llm_trial_flow.trial_service, "update_intake", fake_update)

    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_groups_info", lambda bot_name, audience=None: [])

    reply = LlmTrialFlowHandler().handle(
        "chat-1", "7700", "dopsy_boxing", "сынақ сабағына жазылғым келеді", [], "kk",
    )

    assert not saved.get("child_name")
    assert reply == BOXING_T["ask_name"]["kk"]


_ADULT_SLOTS = [
    {"group_id": 1, "date": "2026-10-24", "time_start": "12:00", "time_end": "13:00",
     "trainer": "Бейсенов Тимур", "training_day": 5},
    {"group_id": 2, "date": "2026-10-23", "time_start": "20:00", "time_end": "21:30",
     "trainer": "Махмуд Мади", "training_day": 4},
]


def test_adult_list_shows_trainers_and_narrows_to_the_wished_one(monkeypatch):
    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_adult_trial_slots", lambda bot: list(_ADULT_SLOTS))
    draft = {"id": 5, "audience": "adult", "child_name": "Ерлан", "child_birth_year": 1996}
    handler = LlmTrialFlowHandler()

    full = handler.pending_prompt("dopsy_boxing", draft, "kk")
    only_timur = handler.pending_prompt("dopsy_boxing", {**draft, "preferred_trainers": ["Timur"]}, "kk")

    assert "— Бейсенов Тимур" in full and "— Махмуд Мади" in full
    assert "📅 Сб 24.10.2026\n1. 12:00 — Бейсенов Тимур" in only_timur
    assert "Махмуд" not in only_timur


def test_confirmation_shows_the_trainer():
    text = llm_trial_flow._confirmation(_CHOSEN_DRAFT, "kk", "Бейсенов Тимур")
    lines = text.split("\n")
    assert lines[lines.index(next(l for l in lines if l.startswith("⏰"))) + 1] == \
        "👨‍🏫 Жаттықтырушы: Бейсенов Тимур"
    assert "Жаттықтырушы" not in llm_trial_flow._confirmation(_CHOSEN_DRAFT, "kk")


def test_a_list_the_last_message_showed_is_not_repeated():
    shown = "Ересектер тобының жаттығулары:\n1. Сб 24.10.2026 — 12:00\n\nҚай күні?"
    history = [{"role": "assistant", "content": f"Сынақ сабағы тегін 🥊\n\n{shown}"}]

    assert llm_trial_flow.shorten_repeated_list(shown, history, "kk") == T["choose_from_above"]["kk"]
    assert llm_trial_flow.shorten_repeated_list(shown, [], "kk") == shown
    # Not a numbered list (e.g. the name question) — asked again in full.
    assert llm_trial_flow.shorten_repeated_list("Есіміңіз кім?", [
        {"role": "assistant", "content": "Есіміңіз кім?"}], "kk") == "Есіміңіз кім?"
