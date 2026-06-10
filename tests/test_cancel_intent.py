"""Cancel-intent detection for the in-flight booking flow."""

import pytest

from handlers.sessions.booking_session import BookingPromptBuilder


@pytest.mark.parametrize("text", [
    "отмена",
    "отмените пожалуйста",
    "Спасибо, но мы не хотим играть",
    "Передумал, не нужно",
    "Стоп, отбой",
    "ой не надо",
    "забудь про эту бронь",
    "тоқтат",
    "керек емес",
])
def test_cancel_detected(text):
    builder = BookingPromptBuilder('dopsy_bot')
    assert builder.is_cancel_intent(text)


@pytest.mark.parametrize("text", [
    "1",                      # step_date number
    "10:00 до 12:00",         # step_time
    "8",                      # step_players
    "Алмаз",                  # step_name
    "да",                     # step_confirm yes
    "нет",                    # step_confirm no — not "strong cancel"
])
def test_normal_inputs_pass_through(text):
    builder = BookingPromptBuilder('dopsy_bot')
    assert not builder.is_cancel_intent(text)
