"""Declines, and «not my child» sentences."""
import pytest

from handlers import llm_trial_flow, message_handler
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


@pytest.mark.parametrize("text", ["балам емес, өзім қатысамын", "не для ребёнка, хочу заниматься сам"])
def test_not_a_child_means_the_adult_group(text):
    assert llm_trial_flow._detect_audience(text) == "adult"


def test_sentence_about_who_attends_is_not_a_name():
    assert not llm_trial_flow._is_plausible_child_name("балам емес, өзім қатысамын")


def test_age_with_locative_suffix_is_read():
    assert 15 in llm_trial_flow._ages_in("менің жасым 15те")
