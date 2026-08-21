from handlers.llm_trial_flow import (
    _session_interrupt_response,
    is_acknowledgement,
    is_bot_identity_question,
    is_greeting,
)


def test_trial_greeting_guard_matches_plain_greetings():
    assert is_greeting("hello")
    assert is_greeting("Hello!")
    assert is_greeting("привет")
    assert is_greeting("сәлем")
    assert is_greeting("Сәлеметсіз бе")
    assert is_greeting("че там")
    assert is_greeting("чё там")
    assert is_greeting("как дела")


def test_trial_greeting_guard_does_not_match_signup_details():
    assert not is_greeting("2016")
    assert not is_greeting("Али")
    assert not is_greeting("хочу записаться")


def test_bot_identity_question_guard():
    assert is_bot_identity_question("что это за бот?")
    assert is_bot_identity_question("кто ты")
    assert is_bot_identity_question("что ты умеешь")
    assert not is_bot_identity_question("хочу записаться")


def test_trial_intake_identity_interrupt_reasks_pending_field():
    reply = _session_interrupt_response("ru", "а что это за бот", "child_name")

    assert "бот-ассистент академии" in reply
    assert "Укажите имя ребенка" in reply


def test_trial_acknowledgement_guard():
    assert is_acknowledgement("понял")
    assert is_acknowledgement("ок")
    assert is_acknowledgement("рахмет")
    assert not is_acknowledgement("Али")
