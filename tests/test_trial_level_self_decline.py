"""Level clarification, self-signup wording and declines."""
import pytest

from handlers import llm_trial_flow, message_handler
from tests.test_academy_unclear import _text, academy  # noqa: F401
from tests.test_trial_intake_order import flow  # noqa: F401

pytestmark = pytest.mark.no_db

_KNOWN = {"child_birth_year": 2016, "child_name": "Айдос"}


def test_never_trained_is_beginner_with_a_note(flow):
    run, state = flow
    run("Сынақ сабағына жазылғым келеді", extracted=_KNOWN)

    reply = run("Бұрын боксқа қатыспаған")

    assert state["draft"]["experience"] == "Beginner"
    assert reply.startswith(llm_trial_flow.T["beginner_note"]["kk"])


def test_trained_before_gets_one_clarification_then_moves_on(flow, monkeypatch):
    run, state = flow
    run("Сынақ сабағына жазылғым келеді", extracted=_KNOWN)

    first = run("Бұрын қатысқан")
    assert first == llm_trial_flow.T["experience_clarify"]["kk"]

    history = [{"role": "user", "content": "Бұрын қатысқан"}, {"role": "assistant", "content": first}]
    monkeypatch.setattr(llm_trial_flow, "extract_trial_details", lambda h, t: dict(llm_trial_flow_empty()))
    second = llm_trial_flow.LlmTrialFlowHandler().handle("chat-1", "7700", "dopsy_boxing", "білмеймін", history, "kk")

    assert state["draft"]["experience"] == "Intermediate"
    assert "қанша уақыт" not in second and "деңгейі" not in second


def test_clarification_number_maps_to_level(flow):
    run, state = flow
    run("Сынақ сабағына жазылғым келеді", extracted=_KNOWN)
    run("Бұрын қатысқан")

    run("3")

    assert state["draft"]["experience"] == "Advanced"


def test_self_signup_is_worded_for_you(flow):
    run, _ = flow

    reply = run("Маған 16 жас, өзім жазылғым келеді")

    assert reply.endswith(llm_trial_flow.BOXING_T["ask_name_self"]["kk"])
    history = [{"role": "user", "content": "Маған 16 жас, өзім жазылғым келеді"},
               {"role": "assistant", "content": reply}]
    reply = llm_trial_flow.LlmTrialFlowHandler().handle("chat-1", "7700", "dopsy_boxing", "Айдос", history, "kk")
    assert reply == llm_trial_flow.BOXING_T["ask_experience_self"]["kk"]
    assert "Балаңыз" not in reply


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


def llm_trial_flow_empty():
    from tests.test_trial_intake_order import _EMPTY
    return _EMPTY


@pytest.mark.parametrize("text", ["балам емес, өзім қатысамын", "менің жасым 15те, өзім қатысамын",
                                  "не для ребёнка, хочу заниматься сам"])
def test_not_a_child_means_self_signup(text):
    assert llm_trial_flow._signup_actor(text) == "self"
    assert llm_trial_flow._detect_audience("балам емес") != "child"


def test_sentence_about_who_attends_is_not_a_name():
    assert not llm_trial_flow._is_plausible_child_name("балам емес, өзім қатысамын")


def test_age_with_locative_suffix_is_read():
    assert 15 in llm_trial_flow._ages_in("менің жасым 15те")
