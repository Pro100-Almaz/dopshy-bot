from datetime import date, time

from handlers import edit_trial
from handlers.llm_trial_flow import LlmTrialFlowHandler


def test_trial_select_slot_digit_selects_slot_without_reextracting(monkeypatch):
    monkeypatch.setattr(
        "handlers.llm_trial_flow._extract_user_data",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not extract slot number")),
    )
    monkeypatch.setattr(
        "handlers.llm_trial_flow.academy_repo.get_trial",
        lambda trial_id: {"id": trial_id},
    )

    def fake_assign(self, chat_id, bot_name, draft, slot, lang):
        assert slot["group_id"] == 7
        return "confirm"

    monkeypatch.setattr(LlmTrialFlowHandler, "_assign_slot_and_confirm", fake_assign)

    reply = LlmTrialFlowHandler().handle_session_turn(
        "chat-1",
        "7700",
        "dopsy_fs_school",
        "1",
        [],
        {
            "state": "trial_select_slot",
            "params": {
                "trial_id": 11,
                "lang": "ru",
                "slots": [
                    {
                        "group_id": 7,
                        "date": "2026-08-24",
                        "time_start": "18:00",
                        "time_end": "19:30",
                    }
                ],
            },
        },
    )

    assert reply == "confirm"


def test_trial_confirm_edit_updates_draft_and_reasks_confirmation(monkeypatch):
    monkeypatch.setattr(
        "handlers.llm_trial_flow._extract_user_data",
        lambda *args, **kwargs: {"school_shift": "morning"},
    )
    monkeypatch.setattr(
        "handlers.llm_trial_flow.trial_service.update_intake",
        lambda bot_name, trial_id, fields: {
            "ok": True,
            "data": {
                "trial": {
                    "id": trial_id,
                    "trial_day": date(2026, 8, 24),
                    "start_time": time(18, 0),
                    "end_time": time(19, 30),
                    "child_name": "Ерсултан",
                    "child_birth_year": 2011,
                    "experience": "Beginner",
                    "school_shift": fields["school_shift"],
                }
            },
        },
    )

    reply = LlmTrialFlowHandler().handle_session_turn(
        "chat-1",
        "7700",
        "dopsy_fs_school",
        "поменяйте смену на утреннюю",
        [],
        {"state": "trial_confirm", "params": {"trial_id": 11, "lang": "ru"}},
    )

    assert "Данные обновил" in reply
    assert "🏫 Смена: утренняя" in reply
    assert "Ответьте *да* или *нет*" in reply


def test_trial_confirm_yes_clears_ineligible_slot_and_reselects(monkeypatch):
    updated = {}
    monkeypatch.setattr(
        "handlers.llm_trial_flow.academy_repo.get_trial",
        lambda trial_id: {
            "id": trial_id,
            "trial_day": date(2026, 8, 24),
            "start_time": time(18, 0),
            "end_time": time(19, 30),
            "group_id": 7,
            "child_name": "Ерсултан",
            "child_birth_year": 2011,
            "experience": "Beginner",
            "school_shift": "morning",
        },
    )
    monkeypatch.setattr(
        "handlers.llm_trial_flow.trial_logic.is_trial_slot_eligible",
        lambda bot_name, draft: False,
    )

    def fake_update(bot_name, trial_id, fields):
        updated.update(fields)
        return {
            "ok": True,
            "data": {
                "trial": {
                    "id": trial_id,
                    "child_name": "Ерсултан",
                    "child_birth_year": 2011,
                    "experience": "Beginner",
                    "school_shift": "morning",
                }
            },
        }

    monkeypatch.setattr("handlers.llm_trial_flow.trial_service.update_intake", fake_update)
    monkeypatch.setattr(
        LlmTrialFlowHandler,
        "_continue_from_draft",
        lambda self, chat_id, bot_name, draft, lang, signup_actor=None: "choose new group",
    )

    reply = LlmTrialFlowHandler().handle_session_turn(
        "chat-1",
        "7700",
        "dopsy_fs_school",
        "да",
        [],
        {"state": "trial_confirm", "params": {"trial_id": 11, "lang": "ru"}},
    )

    assert updated["group_id"] is None
    assert updated["trial_day"] is None
    assert "выберите подходящий вариант заново" in reply
    assert "choose new group" in reply


def test_confirmed_edit_uses_replacement_draft_flow(monkeypatch):
    monkeypatch.setattr(
        edit_trial,
        "extract_trial_details",
        lambda history, user_text: {"school_shift": "morning"},
    )
    monkeypatch.setattr(
        edit_trial,
        "_active_trials",
        lambda bot_name, sender_phone: [{"id": 11, "state": "confirmed"}],
    )
    monkeypatch.setattr(
        edit_trial.trial_service,
        "replace_confirmed_trial_with_draft",
        lambda bot_name, chat_id, trial_id, patch, lang: {
            "ok": True,
            "data": {"trial": {"id": 22, "child_birth_year": 2011, "experience": "Beginner", "school_shift": "morning", "child_name": "Ерсултан"}},
        },
    )
    monkeypatch.setattr(
        LlmTrialFlowHandler,
        "_continue_from_draft",
        lambda self, chat_id, bot_name, draft, lang, signup_actor=None: "choose group",
    )

    reply = edit_trial.handle_edit_request(
        "chat-1",
        "7700",
        {},
        "dopsy_fs_school",
        "поменяйте смену на утреннюю",
        [],
        "ru",
    )

    assert "Подтвержденную запись нельзя изменить напрямую" in reply
    assert "создал новый черновик" in reply
    assert "choose group" in reply
