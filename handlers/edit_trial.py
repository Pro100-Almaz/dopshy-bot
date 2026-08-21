"""Client self-service handlers for academy trial status, edit, and cancel."""

import logging
from datetime import date, datetime

from handlers.academy_extractor import extract_trial_details
from integrations import trial_service
from integrations.repo import postgres

logger = logging.getLogger(__name__)

WEEKDAY = {
    "ru": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
    "kk": ["Дс", "Сс", "Ср", "Бс", "Жм", "Сб", "Жс"],
}


def _bilingual(ru: str, kk: str) -> str:
    return f"{ru}\n\n— — —\n\n{kk}"


def _fmt_date(value, lang: str = "ru") -> str:
    if not value:
        return "?"
    d = value if isinstance(value, date) else datetime.strptime(str(value), "%Y-%m-%d").date()
    return f"{WEEKDAY[lang][d.weekday()]} {d.strftime('%d.%m.%Y')}"


def _trial_line(trial: dict, idx: int | None = None, lang: str = "ru") -> str:
    prefix = f"{idx}. " if idx is not None else ""
    return (
        f"{prefix}{_fmt_date(trial.get('trial_day'), lang)} "
        f"{str(trial.get('start_time') or '?')[:5]}-{str(trial.get('end_time') or '?')[:5]} | "
        f"{trial.get('child_name') or '?'} | {trial.get('state')}"
    )


def _active_trials(bot_name: str, sender_phone: str) -> list[dict]:
    return trial_service.get_active_trials(bot_name, sender_phone)["data"]["trials"]


def handle_trial_status_request(sender_phone: str, bot_name: str, lang: str = "ru") -> str:
    trials = _active_trials(bot_name, sender_phone)
    if not trials:
        return _bilingual(
            "У вас нет активной записи на пробное занятие.",
            "Сізде сынақ сабағына белсенді жазылым жоқ.",
        )
    ru = "Ваши записи:\n" + "\n".join(_trial_line(t, i, "ru") for i, t in enumerate(trials, 1))
    kk = "Сіздің жазылымдарыңыз:\n" + "\n".join(_trial_line(t, i, "kk") for i, t in enumerate(trials, 1))
    return _bilingual(ru, kk)


def handle_cancel_trial_request(chat_id: str, sender_phone: str, bot_name: str) -> str:
    trials = _active_trials(bot_name, sender_phone)
    postgres.delete_session(bot_name, chat_id)

    if not trials:
        return _bilingual(
            "Записи не найдены на ваш номер.",
            "Сіздің нөміріңізге сабаққа жазылым табылмады.",
        )
    if len(trials) == 1:
        trial_service.cancel_trial(bot_name, chat_id, trials[0]["id"], "user_cancel_trial")
        return _bilingual(
            "Ваша запись на пробное занятие отменена.",
            "Сынақ сабағына жазылымыңыз тоқтатылды.",
        )

    postgres.upsert_session(
        bot_name,
        chat_id,
        "trial_cancel_select",
        {"trial_ids": [t["id"] for t in trials], "lang": "ru"},
        None,
    )
    return _bilingual(
        "У вас несколько записей. Введите номер записи для отмены:\n"
        + "\n".join(_trial_line(t, i, "ru") for i, t in enumerate(trials, 1)),
        "Сізде бірнеше жазылым бар. Қай жазылымды тоқтатасыз, нөмірін жазыңыз:\n"
        + "\n".join(_trial_line(t, i, "kk") for i, t in enumerate(trials, 1)),
    )


def handle_cancel_selection(chat_id: str, bot_name: str, user_text: str, params: dict) -> str:
    trial_ids = params.get("trial_ids") or []
    if not user_text.strip().isdigit():
        return _bilingual("Введите номер из списка.", "Тізімдегі нөмірді енгізіңіз.")
    idx = int(user_text.strip()) - 1
    if idx < 0 or idx >= len(trial_ids):
        return _bilingual("Введите номер из списка.", "Тізімдегі нөмірді енгізіңіз.")
    trial_service.cancel_trial(bot_name, chat_id, trial_ids[idx], "user_cancel_trial_selected")
    return _bilingual(
        "Ваша запись на пробное занятие отменена.",
        "Сынақ сабағына жазылымыңыз тоқтатылды.",
    )


def handle_edit_request(
    chat_id: str,
    sender_phone: str,
    diff: dict,
    bot_name: str,
    user_text: str | None = None,
    history: list | None = None,
    lang: str = "ru",
) -> str:
    extracted = extract_trial_details(history or [], user_text or "") if user_text else {}
    diff = {**(diff or {}), **{k: v for k, v in extracted.items() if v not in (None, "")}}
    diff = {k: v for k, v in diff.items() if v not in (None, "")}

    trials = [t for t in _active_trials(bot_name, sender_phone) if t["state"] == "confirmed"]
    if not trials:
        return _bilingual(
            "У вас нет активной записи на пробный урок, которую можно изменить.",
            "Сізде өзгертуге болатын белсенді сынақ сабағы жоқ.",
        )
    if len(trials) > 1:
        return _bilingual(
            "У вас несколько записей. Пока изменение через бот доступно только если запись одна.",
            "Сізде бірнеше жазылым бар. Әзірге бот арқылы өзгерту тек бір жазылым болса қолжетімді.",
        )
    if not diff:
        return _bilingual(
            "Я не понял, что именно изменить. Напишите новые данные.",
            "Нақты не өзгерту керек екенін түсінбедім. Жаңа деректерді жазыңыз.",
        )

    target = trials[0]
    allowed = {
        "child_name", "child_birth_year", "experience", "school_shift",
        "preferred_date", "preferred_weekday", "preferred_time_start",
        "preferred_time_end",
    }
    patch = {k: v for k, v in diff.items() if k in allowed}
    if not patch:
        return _bilingual(
            "Я не понял, что именно изменить. Напишите новые данные.",
            "Нақты не өзгерту керек екенін түсінбедім. Жаңа деректерді жазыңыз.",
        )

    eligibility_keys = {
        "child_birth_year", "experience", "school_shift",
        "preferred_date", "preferred_weekday", "preferred_time_start",
        "preferred_time_end",
    }
    if eligibility_keys.intersection(patch):
        result = trial_service.reopen_confirmed_trial_for_reassignment(bot_name, target["id"], patch)
        if not result["ok"]:
            return _bilingual("Не удалось изменить запись.", "Жазылымды өзгерту мүмкін болмады.")
        from handlers.llm_trial_flow import LlmTrialFlowHandler

        return LlmTrialFlowHandler().handle(
            chat_id, sender_phone, bot_name, user_text or "", history or [], lang
        )

    result = trial_service.update_confirmed_trial(bot_name, target["id"], patch)
    if not result["ok"]:
        return _bilingual("Не удалось изменить запись.", "Жазылымды өзгерту мүмкін болмады.")
    trial = result["data"]["trial"]
    return _bilingual(
        f"✅ Запись обновлена.\n{_trial_line(trial, None, 'ru')}",
        f"✅ Жазылым жаңартылды.\n{_trial_line(trial, None, 'kk')}",
    )
