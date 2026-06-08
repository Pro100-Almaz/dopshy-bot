"""Cancel-intent detection for the in-flight booking flow."""

import pytest

from handlers.sessions.booking_session import _is_cancel_intent


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
    assert _is_cancel_intent(text)


@pytest.mark.parametrize("text", [
    "1",                      # step_date number
    "10:00 до 12:00",         # step_time
    "8",                      # step_players
    "Алмаз",                  # step_name
    "да",                     # step_confirm yes
    "нет",                    # step_confirm no — not "strong cancel"
])
def test_normal_inputs_pass_through(text):
    assert not _is_cancel_intent(text)
