from datetime import date, time

from handlers import edit_trial


def test_trial_status_confirmed_is_localized_ru(monkeypatch):
    monkeypatch.setattr(
        edit_trial,
        "_active_trials",
        lambda bot_name, sender_phone: [
            {
                "state": "confirmed",
                "trial_day": date(2026, 8, 24),
                "start_time": time(18, 0),
                "end_time": time(19, 30),
                "child_name": "Ерсултан",
                "child_birth_year": 2011,
                "experience": "Beginner",
                "school_shift": "morning",
            }
        ],
    )

    reply = edit_trial.handle_trial_status_request("7700", "dopsy_fs_school", "ru")

    assert "У вас есть подтвержденная запись:" in reply
    assert "Ерсултан" in reply
    assert "draft" not in reply
    assert "confirmed" not in reply


def test_trial_status_draft_lists_filled_and_missing_ru(monkeypatch):
    monkeypatch.setattr(
        edit_trial,
        "_active_trials",
        lambda bot_name, sender_phone: [
            {
                "state": "draft",
                "trial_day": None,
                "start_time": None,
                "end_time": None,
                "child_name": "Ерсултан",
                "child_birth_year": 2011,
                "experience": "Beginner",
                "school_shift": None,
            }
        ],
    )

    reply = edit_trial.handle_trial_status_request("7700", "dopsy_fs_school", "ru")

    assert "У вас есть незаконченный черновик записи:" in reply
    assert "Заполнено:" in reply
    assert "имя: Ерсултан" in reply
    assert "Нужно заполнить:" in reply
    assert "школьная смена" in reply
    assert "время пробного занятия" in reply
    assert "draft" not in reply


def test_trial_status_draft_is_localized_kk(monkeypatch):
    monkeypatch.setattr(
        edit_trial,
        "_active_trials",
        lambda bot_name, sender_phone: [
            {
                "state": "draft",
                "child_name": "Ерсултан",
                "child_birth_year": None,
                "experience": None,
                "school_shift": None,
            }
        ],
    )

    reply = edit_trial.handle_trial_status_request("7700", "dopsy_fs_school", "kk")

    assert "Сізде аяқталмаған жазылым жобасы бар:" in reply
    assert "Толтырылған:" in reply
    assert "Толтыру керек:" in reply
    assert "draft" not in reply
    assert "confirmed" not in reply
