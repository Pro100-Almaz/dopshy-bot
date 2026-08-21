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

LEVEL_LABELS = {
    "ru": {
        "Beginner": "начинающий",
        "Intermediate": "средний",
        "Advanced": "продвинутый",
    },
    "kk": {
        "Beginner": "бастапқы",
        "Intermediate": "орта",
        "Advanced": "жоғары",
    },
}

SHIFT_LABELS = {
    "ru": {
        "morning": "утренняя",
        "afternoon": "дневная",
    },
    "kk": {
        "morning": "таңғы",
        "afternoon": "түскі",
    },
}


def _bilingual(ru: str, kk: str) -> str:
    return f"{ru}\n\n— — —\n\n{kk}"


def _fmt_date(value, lang: str = "ru") -> str:
    if not value:
        return "?"
    d = value if isinstance(value, date) else datetime.strptime(str(value), "%Y-%m-%d").date()
    return f"{WEEKDAY[lang][d.weekday()]} {d.strftime('%d.%m.%Y')}"


def _fmt_level(value, lang: str) -> str:
    return LEVEL_LABELS.get(lang, LEVEL_LABELS["ru"]).get(value, value or "?")


def _fmt_shift(value, lang: str) -> str:
    return SHIFT_LABELS.get(lang, SHIFT_LABELS["ru"]).get(value, value or "?")


def _trial_line(trial: dict, idx: int | None = None, lang: str = "ru") -> str:
    prefix = f"{idx}. " if idx is not None else ""
    return (
        f"{prefix}{_fmt_date(trial.get('trial_day'), lang)} "
        f"{str(trial.get('start_time') or '?')[:5]}-{str(trial.get('end_time') or '?')[:5]} | "
        f"{trial.get('child_name') or '?'}"
    )


def _trial_details(trial: dict, idx: int | None = None, lang: str = "ru") -> str:
    prefix = f"{idx}. " if idx is not None else ""
    labels = {
        "ru": {
            "date": "Дата",
            "time": "Время",
            "name": "Имя",
            "birth_year": "Год рождения",
            "level": "Уровень",
            "shift": "Школьная смена",
        },
        "kk": {
            "date": "Күні",
            "time": "Уақыты",
            "name": "Аты",
            "birth_year": "Туған жылы",
            "level": "Деңгейі",
            "shift": "Мектеп ауысымы",
        },
    }[lang]
    time_text = (
        f"{str(trial.get('start_time'))[:5]}-{str(trial.get('end_time'))[:5]}"
        if trial.get("start_time") and trial.get("end_time")
        else "?"
    )
    lines = [
        f"{prefix}{labels['date']}: {_fmt_date(trial.get('trial_day'), lang)}",
        f"{labels['time']}: {time_text}",
        f"{labels['name']}: {trial.get('child_name') or '?'}",
        f"{labels['birth_year']}: {trial.get('child_birth_year') or '?'}",
        f"{labels['level']}: {_fmt_level(trial.get('experience'), lang)}",
        f"{labels['shift']}: {_fmt_shift(trial.get('school_shift'), lang)}",
    ]
    return "\n".join(lines)


def _draft_summary(trial: dict, idx: int | None = None, lang: str = "ru") -> str:
    labels = {
        "ru": {
            "item": {
                "child_name": "имя",
                "child_birth_year": "год рождения",
                "experience": "уровень подготовки",
                "school_shift": "школьная смена",
                "slot": "время пробного занятия",
            },
            "filled": "Заполнено",
            "missing": "Нужно заполнить",
            "nothing": "пока ничего",
        },
        "kk": {
            "item": {
                "child_name": "аты",
                "child_birth_year": "туған жылы",
                "experience": "дайындық деңгейі",
                "school_shift": "мектеп ауысымы",
                "slot": "сынақ сабағының уақыты",
            },
            "filled": "Толтырылған",
            "missing": "Толтыру керек",
            "nothing": "әзірге ештеңе жоқ",
        },
    }[lang]
    values = {
        "child_name": trial.get("child_name"),
        "child_birth_year": trial.get("child_birth_year"),
        "experience": _fmt_level(trial.get("experience"), lang) if trial.get("experience") else None,
        "school_shift": _fmt_shift(trial.get("school_shift"), lang) if trial.get("school_shift") else None,
        "slot": (
            f"{_fmt_date(trial.get('trial_day'), lang)} "
            f"{str(trial.get('start_time'))[:5]}-{str(trial.get('end_time'))[:5]}"
            if trial.get("trial_day") and trial.get("start_time") and trial.get("end_time")
            else None
        ),
    }
    filled = [
        f"{labels['item'][key]}: {value}"
        for key, value in values.items()
        if value not in (None, "")
    ]
    missing = [
        labels["item"][key]
        for key, value in values.items()
        if value in (None, "")
    ]
    prefix = f"{idx}. " if idx is not None else ""
    return (
        f"{prefix}{labels['filled']}: {', '.join(filled) if filled else labels['nothing']}.\n"
        f"{labels['missing']}: {', '.join(missing) if missing else '-'}."
    )


def _active_trials(bot_name: str, sender_phone: str) -> list[dict]:
    return trial_service.get_active_trials(bot_name, sender_phone)["data"]["trials"]


def _normalize_edit_patch(diff: dict, extracted: dict) -> dict:
    merged = {**(diff or {}), **{k: v for k, v in (extracted or {}).items() if v not in (None, "")}}
    if "shift" in merged and "school_shift" not in merged:
        merged["school_shift"] = merged["shift"]
    if merged.get("school_shift") not in (None, "", "morning", "afternoon"):
        value = str(merged["school_shift"]).strip().lower()
        if "утрен" in value or "утром" in value or "таң" in value:
            merged["school_shift"] = "morning"
        elif "дневн" in value or "днем" in value or "түск" in value:
            merged["school_shift"] = "afternoon"
    if merged.get("experience") not in (None, "", "Beginner", "Intermediate", "Advanced"):
        value = str(merged["experience"]).strip().lower()
        if "начина" in value or "нович" in value or "бастап" in value:
            merged["experience"] = "Beginner"
        elif "средн" in value or "орта" in value:
            merged["experience"] = "Intermediate"
        elif "продвин" in value or "жоғары" in value:
            merged["experience"] = "Advanced"
    if "trial_day" in merged and "preferred_weekday" not in merged:
        try:
            merged["preferred_weekday"] = int(merged["trial_day"]) - 1
        except (TypeError, ValueError):
            pass
    if "trial_time" in merged and "preferred_time_start" not in merged:
        merged["preferred_time_start"] = str(merged["trial_time"])[:5]
    allowed = {
        "child_name", "child_birth_year", "experience", "school_shift",
        "preferred_date", "preferred_weekday", "preferred_time_start",
        "preferred_time_end",
    }
    patch = {key: value for key, value in merged.items() if key in allowed and value not in (None, "")}
    if patch.get("school_shift") not in (None, "morning", "afternoon"):
        patch.pop("school_shift", None)
    if patch.get("experience") not in (None, "Beginner", "Intermediate", "Advanced"):
        patch.pop("experience", None)
    return patch


def handle_trial_status_request(sender_phone: str, bot_name: str, lang: str = "ru") -> str:
    trials = _active_trials(bot_name, sender_phone)
    if not trials:
        return (
            "Сізде сынақ сабағына белсенді жазылым жоқ."
            if lang == "kk"
            else "У вас нет активной записи на пробное занятие."
        )

    confirmed = [t for t in trials if t.get("state") == "confirmed"]
    drafts = [t for t in trials if t.get("state") == "draft"]
    parts = []
    if confirmed:
        if lang == "kk":
            header = "Сізде расталған жазылым бар:" if len(confirmed) == 1 else "Сізде расталған жазылымдар бар:"
        else:
            header = "У вас есть подтвержденная запись:" if len(confirmed) == 1 else "У вас есть подтвержденные записи:"
        parts.append(header + "\n" + "\n\n".join(_trial_details(t, i, lang) for i, t in enumerate(confirmed, 1)))
    if drafts:
        if lang == "kk":
            header = "Сізде аяқталмаған жазылым жобасы бар:" if len(drafts) == 1 else "Сізде аяқталмаған жазылым жобалары бар:"
        else:
            header = "У вас есть незаконченный черновик записи:" if len(drafts) == 1 else "У вас есть незаконченные черновики записей:"
        parts.append(header + "\n" + "\n\n".join(_draft_summary(t, i, lang) for i, t in enumerate(drafts, 1)))
    return "\n\n".join(parts)


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
    patch = _normalize_edit_patch(diff or {}, extracted)

    active_trials = _active_trials(bot_name, sender_phone)
    drafts = [t for t in active_trials if t["state"] == "draft"]
    confirmed_trials = [t for t in active_trials if t["state"] == "confirmed"]

    if drafts and not confirmed_trials:
        if not patch:
            return _bilingual(
                "Я не понял, что именно изменить. Напишите новые данные.",
                "Нақты не өзгерту керек екенін түсінбедім. Жаңа деректерді жазыңыз.",
            )
        from handlers.llm_trial_flow import LlmTrialFlowHandler

        return LlmTrialFlowHandler().handle(
            chat_id, sender_phone, bot_name, user_text or "", history or [], lang
        )

    if not confirmed_trials:
        return _bilingual(
            "У вас нет активной записи на пробный урок, которую можно изменить.",
            "Сізде өзгертуге болатын белсенді сынақ сабағы жоқ.",
        )
    if len(confirmed_trials) > 1:
        return _bilingual(
            "У вас несколько записей. Пока изменение через бот доступно только если запись одна.",
            "Сізде бірнеше жазылым бар. Әзірге бот арқылы өзгерту тек бір жазылым болса қолжетімді.",
        )
    if not patch:
        return _bilingual(
            "Я не понял, что именно изменить. Напишите новые данные.",
            "Нақты не өзгерту керек екенін түсінбедім. Жаңа деректерді жазыңыз.",
        )

    target = confirmed_trials[0]
    result = trial_service.replace_confirmed_trial_with_draft(
        bot_name, chat_id, target["id"], patch, lang
    )
    if not result["ok"]:
        return _bilingual("Не удалось изменить запись.", "Жазылымды өзгерту мүмкін болмады.")

    from handlers.llm_trial_flow import LlmTrialFlowHandler

    flow_reply = LlmTrialFlowHandler()._continue_from_draft(
        chat_id, bot_name, result["data"]["trial"], lang
    )
    prefix = (
        "Подтвержденную запись нельзя изменить напрямую. Я отменил прежнюю запись и создал новый черновик с обновленными данными."
        if lang == "ru"
        else "Расталған жазылымды тікелей өзгертуге болмайды. Бұрынғы жазылымды тоқтатып, жаңартылған деректермен жаңа жоба жасадым."
    )
    return f"{prefix}\n\n{flow_reply}"
