from datetime import date, time

from handlers.llm_trial_flow import (
    LlmTrialFlowHandler,
    _ask_missing,
    _birth_year_from_age,
    _normalize_manual_value,
)


def test_birth_year_from_self_signup_age():
    assert _birth_year_from_age("мне 15 лет и я хочу записаться", date(2026, 8, 21)) == 2011
    assert _birth_year_from_age("маған 12 жас", date(2026, 8, 21)) == 2014


def test_self_signup_asks_for_user_name():
    assert _ask_missing("ru", "child_name", "self") == "Укажите ваше имя."


def test_school_shift_change_is_detected_inside_sentence():
    assert _normalize_manual_value("school_shift", "поменяйте школьную смену на утреннюю") == "morning"
    assert _normalize_manual_value("school_shift", "давайте дневную смену") == "afternoon"


def test_no_exact_groups_returns_age_filtered_groups_and_keeps_session(monkeypatch):
    upserted = {}

    monkeypatch.setattr(
        "handlers.llm_trial_flow.trial_logic.get_eligible_trial_slots",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "handlers.llm_trial_flow.trial_logic.get_fallback_trial_slots",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "handlers.llm_trial_flow.trial_logic.get_birth_year_trial_slots",
        lambda *args, **kwargs: [
            {
                "group_id": 7,
                "group_name": "U15",
                "training_day": 0,
                "date": date(2026, 8, 24),
                "time_start": time(18, 0),
                "time_end": time(19, 30),
                "level": ["Beginner", "Intermediate"],
            }
        ],
    )

    def fake_upsert(bot_name, chat_id, state, params, object_id):
        upserted.update(
            {
                "bot_name": bot_name,
                "chat_id": chat_id,
                "state": state,
                "params": params,
                "object_id": object_id,
            }
        )

    monkeypatch.setattr("handlers.llm_trial_flow.postgres.upsert_session", fake_upsert)

    reply = LlmTrialFlowHandler()._evaluate_slots(
        "chat-1",
        "dopsy_fs_school",
        {
            "id": 11,
            "child_birth_year": 2011,
            "school_shift": "afternoon",
            "experience": "Beginner",
        },
        "ru",
        "self",
    )

    assert "Подходящих групп сейчас не нашлось" in reply
    assert "По возрасту 2011 подходят такие группы" in reply
    assert "U15" in reply
    assert upserted["state"] == "trial_intake"
    assert upserted["params"]["waiting_for"] is None
    assert upserted["params"]["signup_actor"] == "self"


def test_no_age_filtered_groups_tells_user_to_call_admin(monkeypatch):
    monkeypatch.setattr(
        "handlers.llm_trial_flow.trial_logic.get_eligible_trial_slots",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "handlers.llm_trial_flow.trial_logic.get_fallback_trial_slots",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "handlers.llm_trial_flow.trial_logic.get_birth_year_trial_slots",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr("handlers.llm_trial_flow.postgres.upsert_session", lambda *args, **kwargs: None)

    reply = LlmTrialFlowHandler()._evaluate_slots(
        "chat-1",
        "dopsy_fs_school",
        {
            "id": 11,
            "child_birth_year": 2011,
            "school_shift": "afternoon",
            "experience": "Beginner",
        },
        "ru",
    )

    assert "позвоните администратору" in reply
    assert "+7 700 555 6000" in reply
