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
from handlers.llm_trial_flow import BOXING_T, T, LlmTrialFlowHandler, _confirm_decision

pytestmark = pytest.mark.no_db

_CHOSEN_DRAFT = {
    "id": 11, "trial_day": "2026-09-25", "start_time": "16:00", "end_time": "17:00", "group_id": 7,
    "child_name": "Ерсултан", "child_birth_year": 2016,
    "experience": "Intermediate", "school_shift": "morning",
}


def test_confirm_decision_ignores_da_inside_other_words():
    # "дать" contains "да" as a substring — must not read as "yes".
    assert _confirm_decision("кстати можешь дать информацию по тренерам?") is None
    assert _confirm_decision("да") == "yes"
    assert _confirm_decision("подтверждаю") == "yes"


def test_confirm_decision_recognizes_cancel_stems():
    assert _confirm_decision("хочу отменить") == "no"
    assert _confirm_decision("хочу отменить все") == "no"
    assert _confirm_decision("нет") == "no"


def _with_draft(monkeypatch, draft, extracted=None):
    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_existing_trial_draft",
                        lambda phone, bot_name: dict(draft))
    monkeypatch.setattr(llm_trial_flow, "extract_trial_details",
                        lambda history, user_text: dict(extracted or {}))
    monkeypatch.setattr(llm_trial_flow.trial_service, "update_intake",
                        lambda bot_name, trial_id, fields: {"ok": True, "data": {"trial": {**draft, **fields}}})


def test_unrelated_question_with_a_chosen_class_does_not_autoconfirm(monkeypatch):
    _with_draft(monkeypatch, _CHOSEN_DRAFT)
    monkeypatch.setattr(llm_trial_flow.trial_service, "confirm_trial",
                        lambda *a: pytest.fail("must not confirm"))
    monkeypatch.setattr(llm_trial_flow.trial_logic, "get_eligible_trial_slots", lambda *a, **k: [{
        "group_id": 7, "date": "2026-09-25", "time_start": "16:00", "time_end": "17:00", "level": [],
    }])

    reply = LlmTrialFlowHandler().handle(
        "chat-1", "7700", "dopsy_boxing", "кстати можешь дать информацию по тренерам?", [], "ru",
    )

    assert "Детали записи" in reply


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
        "chat-1", "7700", "dopsy_boxing", "хочу отменить все", [], "ru",
    )

    assert cancelled == {
        "bot_name": "dopsy_boxing", "chat_id": "chat-1",
        "trial_id": 11, "reason": "user_declined_gated_trial",
    }
    assert "отменена" in reply


def test_birth_year_gibberish_is_reasked_not_echoed_back(monkeypatch):
    _with_draft(monkeypatch, {"id": 11, "child_name": "Ерсултан"})

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
    monkeypatch.setattr(llm_trial_flow, "extract_trial_details", lambda history, text: {})

    def fake_update(bot_name, trial_id, fields):
        saved.update(fields)
        return {"ok": True, "data": {"trial": {"id": 11, **fields}}}

    monkeypatch.setattr(llm_trial_flow.trial_service, "update_intake", fake_update)

    reply = LlmTrialFlowHandler().handle(
        "chat-1", "7700", "dopsy_boxing", "Балама сынақ сабағы керек", [], "kk",
    )

    assert saved["child_name"] is None
    assert reply == BOXING_T["ask_name"]["kk"]


def test_sentence_on_return_to_a_stale_draft_is_not_taken_as_the_name(monkeypatch):
    from datetime import datetime, timedelta, timezone

    stale = {"id": 11, "updated_at": datetime.now(timezone.utc) - timedelta(days=14)}
    saved = {}
    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_existing_trial_draft", lambda phone, bot: dict(stale))
    monkeypatch.setattr(llm_trial_flow, "extract_trial_details", lambda history, text: {})

    def fake_update(bot_name, trial_id, fields):
        saved.update(fields)
        return {"ok": True, "data": {"trial": {**stale, **fields}}}

    monkeypatch.setattr(llm_trial_flow.trial_service, "update_intake", fake_update)

    reply = LlmTrialFlowHandler().handle(
        "chat-1", "7700", "dopsy_boxing", "сынақ сабағына жазылғым келеді", [], "kk",
    )

    assert saved["child_name"] is None
    assert reply == BOXING_T["ask_name"]["kk"]


def test_my_trial_query_reports_status_instead_of_asking_for_birth_year(monkeypatch):
    monkeypatch.setattr(
        "handlers.edit_trial.handle_trial_status_request",
        lambda sender_phone, bot_name, lang: "У вас нет активной записи на пробное занятие.",
    )

    reply = LlmTrialFlowHandler().handle(
        "chat-1", "7700", "dopsy_boxing", "мои занятия", [], "ru",
    )

    assert "У вас нет активной записи" in reply
    assert "не подходит" not in reply
    assert BOXING_T["ask_birth_year"]["ru"] not in reply
