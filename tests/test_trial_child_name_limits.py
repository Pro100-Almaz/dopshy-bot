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
    saved = _stub_intake(monkeypatch, {})

    reply = LlmTrialFlowHandler().handle(
        "chat-1", "77072479672", "dopsy_fs_school", _LONG_NAME, [], "ru",
    )

    assert not saved.get("child_name")
    assert reply == (
        f"Имя слишком длинное — не больше {CHILD_NAME_MAX_LEN} символов. "
        "Напишите, пожалуйста, короче."
    )


def _stub_intake(monkeypatch, draft_fields, extracted=None):
    """Run LlmTrialFlowHandler.handle against an in-memory draft.

    The field being asked for follows from what `draft_fields` lacks.
    """
    draft = {"id": 3, "state": "draft", **draft_fields}
    saved = {}
    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_existing_trial_draft",
                        lambda phone, bot: draft)
    monkeypatch.setattr(llm_trial_flow, "extract_trial_details",
                        lambda history, text: dict(extracted or {}))

    def fake_update(bot_name, trial_id, fields):
        saved.update(fields)
        return {"ok": True, "data": {"trial": {**draft, **fields}}}

    monkeypatch.setattr(llm_trial_flow.trial_service, "update_intake", fake_update)
    monkeypatch.setattr(llm_trial_flow.academy_repo, "get_groups_info", lambda bot_name=None, audience=None: [])
    return saved


from integrations import trial_service  # noqa: E402


def test_update_intake_refuses_values_longer_than_the_column(monkeypatch, caplog):
    monkeypatch.setattr(trial_service.postgres, "update_draft",
                        lambda *a, **k: pytest.fail("must not reach the database"))

    with caplog.at_level(logging.WARNING, logger="integrations.trial_service"):
        result = trial_service.update_intake("dopsy_fs_school", 3, {"child_name": _LONG_NAME})

    assert result["code"] == "FIELD_TOO_LONG"
    assert result["data"] == {"field": "child_name", "max_len": 30}
    assert "child_name is 31 chars (max 30)" in caplog.text
