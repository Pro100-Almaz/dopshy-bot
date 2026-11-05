import pytest

from handlers.llm_trial_flow import _session_interrupt_response

pytestmark = pytest.mark.no_db


def test_trial_intake_identity_interrupt_reasks_pending_field():
    reply = _session_interrupt_response("ru", "bot_identity", "child_name")

    assert "бот-ассистент академии" in reply
    assert "Укажите имя того, кто будет заниматься" in reply


def test_only_the_small_talk_intents_interrupt():
    assert _session_interrupt_response("ru", "greeting") == "Здравствуйте!"
    assert _session_interrupt_response("ru", "thanks") == "Хорошо."
    assert _session_interrupt_response("ru", "trial_continue") is None
