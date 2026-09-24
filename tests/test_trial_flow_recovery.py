"""Regression tests for the flow getting stuck / misreading side messages.

Real production incidents this covers:
- "кстати можешь дать информацию по тренерам?" during trial_confirm was read
  as a "yes" (the word "да" is a substring of "дать") and silently confirmed
  a booking the user was trying to escape.
- "хочу отменить" / "хочу отменить все" during trial_confirm didn't match the
  cancel word list at all, so cancellation silently failed.
- "что ты несешь?" while asked for child_birth_year got echoed back verbatim
  in the "Год рождения {что ты несешь?} не подходит" message.
- "мои занятия" / "какие есть у меня пробные" during intake was treated as an
  attempted (invalid) value for whatever field was being asked, instead of
  answering the actual question.
- Every reasoned-fallback reply during intake got "Укажите имя ребенка."
  glued on verbatim regardless of context — e.g. right after the bot itself
  asked the user's own age in response to "можно ли записаться взрослому?",
  making the very next line ask for a child's name.
"""

from handlers.llm_trial_flow import LlmTrialFlowHandler, _confirm_decision, _reasoned_fallback


def test_confirm_decision_ignores_da_inside_other_words():
    # "дать" contains "да" as a substring — must not read as "yes".
    assert _confirm_decision("кстати можешь дать информацию по тренерам?") is None
    assert _confirm_decision("да") == "yes"
    assert _confirm_decision("подтверждаю") == "yes"


def test_confirm_decision_recognizes_cancel_stems():
    assert _confirm_decision("хочу отменить") == "no"
    assert _confirm_decision("хочу отменить все") == "no"
    assert _confirm_decision("нет") == "no"


def test_trial_confirm_unrelated_question_gets_reasoned_fallback_not_autoconfirm(monkeypatch):
    monkeypatch.setattr(
        "handlers.llm_trial_flow.academy_repo.get_trial",
        lambda trial_id: {
            "id": trial_id,
            "trial_day": "2026-09-25",
            "start_time": "16:00",
            "end_time": "17:00",
            "child_name": "Ерсултан",
            "child_birth_year": 2016,
            "experience": "Intermediate",
            "school_shift": "morning",
        },
    )
    monkeypatch.setattr(
        "handlers.llm_trial_flow._extract_user_data",
        lambda *args, **kwargs: {},
    )
    calls = {}

    def fake_fallback(bot_name, lang, user_text, reminder):
        calls["args"] = (bot_name, lang, user_text)
        return "FALLBACK_REPLY"

    monkeypatch.setattr("handlers.llm_trial_flow._reasoned_fallback", fake_fallback)

    reply = LlmTrialFlowHandler().handle_session_turn(
        "chat-1", "7700", "dopsy_boxing",
        "кстати можешь дать информацию по тренерам?",
        [],
        {"state": "trial_confirm", "params": {"trial_id": 11, "lang": "ru"}},
    )

    assert reply == "FALLBACK_REPLY"
    assert calls["args"] == ("dopsy_boxing", "ru", "кстати можешь дать информацию по тренерам?")


def test_trial_confirm_cancel_phrase_actually_cancels(monkeypatch):
    monkeypatch.setattr(
        "handlers.llm_trial_flow.academy_repo.get_trial",
        lambda trial_id: {"id": trial_id},
    )
    cancelled = {}
    monkeypatch.setattr(
        "handlers.llm_trial_flow.trial_service.cancel_trial",
        lambda bot_name, chat_id, trial_id, reason: cancelled.update(
            bot_name=bot_name, chat_id=chat_id, trial_id=trial_id, reason=reason
        ),
    )

    reply = LlmTrialFlowHandler().handle_session_turn(
        "chat-1", "7700", "dopsy_boxing", "хочу отменить все", [],
        {"state": "trial_confirm", "params": {"trial_id": 11, "lang": "ru"}},
    )

    assert cancelled == {
        "bot_name": "dopsy_boxing", "chat_id": "chat-1",
        "trial_id": 11, "reason": "user_declined_gated_trial",
    }
    assert "отменена" in reply


def test_birth_year_gibberish_gets_reasoned_fallback_not_echoed_back(monkeypatch):
    monkeypatch.setattr(
        "handlers.llm_trial_flow.academy_repo.get_existing_trial_draft",
        lambda phone, bot_name: {"id": 11, "child_name": "Ерсултан"},
    )
    monkeypatch.setattr(
        "handlers.llm_trial_flow.postgres.get_active_session",
        lambda bot_name, chat_id: {
            "params": {"waiting_for": "child_birth_year", "lang": "ru"}
        },
    )
    monkeypatch.setattr(
        "handlers.llm_trial_flow.extract_trial_details",
        lambda history, user_text: {
            "child_name": None, "child_birth_year": None, "experience": None,
            "school_shift": None, "preferred_date": None, "preferred_weekday": None,
            "preferred_time_start": None, "preferred_time_end": None,
        },
    )
    monkeypatch.setattr(
        "handlers.llm_trial_flow.postgres.upsert_session",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "handlers.llm_trial_flow.trial_service.update_intake",
        lambda bot_name, trial_id, fields: {
            "ok": True,
            "data": {
                "trial": {
                    "id": trial_id, "child_name": "Ерсултан", "child_birth_year": None,
                    "experience": None, "school_shift": None,
                },
            },
        },
    )
    calls = {}

    def fake_fallback(bot_name, lang, user_text, reminder, weave_hint=None):
        calls["user_text"] = user_text
        calls["weave_hint"] = weave_hint
        return "FALLBACK_REPLY"

    monkeypatch.setattr("handlers.llm_trial_flow._reasoned_fallback", fake_fallback)

    reply = LlmTrialFlowHandler().handle(
        "chat-1", "7700", "dopsy_fs_school", "что ты несешь?", [], "ru",
    )

    assert reply == "FALLBACK_REPLY"
    assert calls["user_text"] == "что ты несешь?"
    # A plain intake question gets a weave hint, not a literal glued-on prompt
    # — the model should vary the phrasing instead of repeating "Укажите год
    # рождения ребенка." verbatim on every unrelated message.
    assert calls["weave_hint"] == "нужно узнать год рождения ребёнка"


def test_my_trial_query_reports_status_instead_of_asking_for_birth_year(monkeypatch):
    monkeypatch.setattr(
        "handlers.llm_trial_flow.postgres.get_active_session",
        lambda bot_name, chat_id: {
            "params": {"waiting_for": "child_birth_year", "lang": "ru"}
        },
    )
    monkeypatch.setattr(
        "handlers.edit_trial.handle_trial_status_request",
        lambda sender_phone, bot_name, lang: "У вас нет активной записи на пробное занятие.",
    )

    reply = LlmTrialFlowHandler().handle(
        "chat-1", "7700", "dopsy_boxing", "мои занятия", [], "ru",
    )

    assert "У вас нет активной записи" in reply
    assert "не подходит" not in reply


def test_reasoned_fallback_weave_mode_skips_the_literal_reminder(monkeypatch):
    monkeypatch.setattr("rag.retriever.retrieve_context", lambda query, bot_name=None: "")
    monkeypatch.setattr(
        "chat.llm.get_trial_reply",
        lambda user_text, context="", system_hint="": "МОДЕЛЬНЫЙ ОТВЕТ, включающий напоминание",
    )

    reply = _reasoned_fallback(
        "dopsy_boxing", "ru", "можно ли записаться взрослому?",
        "Укажите имя ребенка.", weave_hint="нужно узнать имя ребёнка",
    )

    # Weave mode: the model's own reply is the whole message — no verbatim
    # "Укажите имя ребенка." appended after it.
    assert reply == "МОДЕЛЬНЫЙ ОТВЕТ, включающий напоминание"
    assert "Укажите имя ребенка." not in reply


def test_reasoned_fallback_append_mode_keeps_structured_reminder_verbatim(monkeypatch):
    monkeypatch.setattr("rag.retriever.retrieve_context", lambda query, bot_name=None: "")
    monkeypatch.setattr(
        "chat.llm.get_trial_reply",
        lambda user_text, context="", system_hint="": "Краткий ответ.",
    )

    reply = _reasoned_fallback(
        "dopsy_boxing", "ru", "а когда вообще тренировки?",
        "1. Пн 18:00\n2. Ср 18:00",
    )

    # No weave_hint: structured content (a slot list) must survive verbatim,
    # not be paraphrased by the model.
    assert "1. Пн 18:00\n2. Ср 18:00" in reply
    assert reply.startswith("Краткий ответ.")
