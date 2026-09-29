import logging

import pytest

from handlers import llm_trial_flow
from handlers.llm_trial_flow import (
    CHILD_NAME_MAX_LEN,
    LlmTrialFlowHandler,
    _child_name_rejection,
    _is_plausible_child_name,
)

# 31 characters, 4 words — the name that overflowed VARCHAR(30) in production.
_LONG_NAME = "лебовский-граф первый ибн Хасан"

pytestmark = pytest.mark.no_db


def test_child_name_rejection_reasons():
    assert _child_name_rejection("Али") is None
    assert _child_name_rejection("а" * CHILD_NAME_MAX_LEN) is None
    assert _child_name_rejection("а" * (CHILD_NAME_MAX_LEN + 1)) == "too_long"
    assert _child_name_rejection(_LONG_NAME) == "too_long"
    assert _child_name_rejection("один два три четыре пять") == "too_many_words"
    assert _child_name_rejection("Али?") == "question"
    assert _child_name_rejection("хочу записаться") == "blocked_phrase"
    assert _child_name_rejection("привет") == "greeting"
    assert _child_name_rejection("   ") == "empty"


def test_rejected_child_name_is_logged_with_reason(caplog):
    with caplog.at_level(logging.INFO, logger="handlers.llm_trial_flow"):
        assert not _is_plausible_child_name(_LONG_NAME)

    assert "child_name rejected: reason=too_long len=31 max_len=30" in caplog.text


def test_too_long_name_is_not_saved_and_client_is_told_why(monkeypatch):
    draft = {"id": 3, "state": "draft"}
    saved = {}

    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_existing_trial_draft",
                        lambda phone, bot: draft)
    monkeypatch.setattr(llm_trial_flow.postgres, "get_active_session",
                        lambda bot, chat: {"params": {"waiting_for": "child_name"}})
    monkeypatch.setattr(llm_trial_flow.postgres, "upsert_session", lambda *a, **k: None)
    monkeypatch.setattr(llm_trial_flow, "extract_trial_details", lambda history, text: {})

    def fake_update(bot_name, trial_id, fields):
        saved.update(fields)
        return {"ok": True, "data": {"trial": {**draft, **fields}}}

    monkeypatch.setattr(llm_trial_flow.trial_service, "update_intake", fake_update)

    reply = LlmTrialFlowHandler().handle(
        "chat-1", "77072479672", "dopsy_fs_school", _LONG_NAME, [], "ru",
    )

    assert saved["child_name"] is None
    assert reply == (
        f"Имя слишком длинное — не больше {CHILD_NAME_MAX_LEN} символов. "
        "Напишите, пожалуйста, короче."
    )


# ---------------------------------------------------------------------------
# Other intake restrictions
# ---------------------------------------------------------------------------

from datetime import date  # noqa: E402

from handlers.llm_trial_flow import (  # noqa: E402
    CHILD_MAX_AGE,
    CHILD_MIN_AGE,
    _birth_year_rejection,
)
from integrations import trial_service  # noqa: E402

_TODAY = date(2026, 9, 23)


def _stub_intake(monkeypatch, waiting_for, extracted=None):
    """Run LlmTrialFlowHandler.handle against in-memory draft/session stubs."""
    draft = {"id": 3, "state": "draft"}
    saved = {}
    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_existing_trial_draft",
                        lambda phone, bot: draft)
    monkeypatch.setattr(llm_trial_flow.postgres, "get_active_session",
                        lambda bot, chat: {"params": {"waiting_for": waiting_for}})
    monkeypatch.setattr(llm_trial_flow.postgres, "upsert_session", lambda *a, **k: None)
    monkeypatch.setattr(llm_trial_flow, "extract_trial_details",
                        lambda history, text: dict(extracted or {}))

    def fake_update(bot_name, trial_id, fields):
        saved.update(fields)
        return {"ok": True, "data": {"trial": {**draft, **fields}}}

    monkeypatch.setattr(llm_trial_flow.trial_service, "update_intake", fake_update)
    return saved


def test_birth_year_rejection_reasons():
    assert _birth_year_rejection(2016, _TODAY) is None
    assert _birth_year_rejection(2026 - CHILD_MIN_AGE, _TODAY) is None
    assert _birth_year_rejection(2026 - CHILD_MAX_AGE, _TODAY) is None
    assert _birth_year_rejection(2027, _TODAY) == "in_future"
    assert _birth_year_rejection(2025, _TODAY) == "too_young"
    assert _birth_year_rejection(1995, _TODAY) == "too_old"
    assert _birth_year_rejection("двадцать", _TODAY) == "not_a_year"


def test_implausible_birth_year_is_not_saved_and_client_is_told_why(monkeypatch, caplog):
    saved = _stub_intake(monkeypatch, "child_birth_year", {"child_name": "Али"})

    with caplog.at_level(logging.INFO, logger="handlers.llm_trial_flow"):
        reply = LlmTrialFlowHandler().handle(
            "chat-1", "77072479672", "dopsy_fs_school", "1995", [], "ru",
        )

    assert saved["child_birth_year"] is None
    assert "child_birth_year rejected: reason=too_old" in caplog.text
    assert reply.startswith("Год рождения 1995 не подходит")


def test_unrecognized_answer_is_logged(monkeypatch, caplog):
    _stub_intake(monkeypatch, "experience", {"child_name": "Али", "child_birth_year": 2016})

    with caplog.at_level(logging.INFO, logger="handlers.llm_trial_flow"):
        LlmTrialFlowHandler().handle(
            "chat-1", "77072479672", "dopsy_fs_school", "не знаю", [], "ru",
        )

    assert "experience rejected: reason=unrecognized value='не знаю'" in caplog.text


def test_update_intake_refuses_values_longer_than_the_column(monkeypatch, caplog):
    monkeypatch.setattr(trial_service.postgres, "update_draft",
                        lambda *a, **k: pytest.fail("must not reach the database"))

    with caplog.at_level(logging.WARNING, logger="integrations.trial_service"):
        result = trial_service.update_intake("dopsy_fs_school", 3, {"child_name": _LONG_NAME})

    assert result["code"] == "FIELD_TOO_LONG"
    assert result["data"] == {"field": "child_name", "max_len": 30}
    assert "child_name is 31 chars (max 30)" in caplog.text


def test_out_of_range_slot_choice_is_logged(caplog):
    session = {"state": "trial_select_slot",
               "params": {"trial_id": 11, "lang": "ru", "slots": [{"group_id": 7}]}}

    with caplog.at_level(logging.INFO, logger="handlers.llm_trial_flow"):
        reply = LlmTrialFlowHandler().handle_session_turn(
            "chat-1", "7700", "dopsy_fs_school", "5", [], session,
        )

    assert reply == "Введите номер из списка доступных вариантов."
    assert "slot_choice rejected: reason=out_of_range trial_id=11 options=1" in caplog.text
