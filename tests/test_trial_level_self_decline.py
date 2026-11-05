"""Declines."""
import pytest

from handlers import message_handler
from tests.test_academy_unclear import _text, academy  # noqa: F401

pytestmark = pytest.mark.no_db


@pytest.mark.parametrize("intent, needle, cancels", [
    ("decline_distance", "орналасқан жеріміз", True),
    ("decline", "Түсінікті", True),
    ("decline_later", "асықпаңыз", False),
])
def test_decline_replies_briefly(academy, monkeypatch, intent, needle, cancels):  # noqa: F811
    cancelled = []
    academy["intent"] = (intent, "kk")
    monkeypatch.setattr(message_handler.academy_repo, "get_existing_trial_draft", lambda *a: {"id": 9})
    monkeypatch.setattr(message_handler.LlmTrialFlowHandler, "pending_prompt", lambda *a: "Балаңыз неше жаста?")
    monkeypatch.setattr(message_handler.trial_service, "cancel_trial", lambda *a: cancelled.append(a))
    monkeypatch.setattr(message_handler, "get_ai_response", lambda **k: pytest.fail("no LLM on a decline"))

    message_handler.handle_incoming_message(_text("Бізге алыс екен"))

    [reply] = academy["sent"]
    assert needle in reply and "?" not in reply
    assert bool(cancelled) is cancels
