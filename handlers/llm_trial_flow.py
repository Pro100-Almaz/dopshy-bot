import logging
from datetime import date, datetime

from handlers.academy_extractor import extract_trial_details
from integrations import trial_service
from integrations import trial as trial_logic
from integrations.repo import academy_repo, postgres

logger = logging.getLogger(__name__)


T = {
    "ask_name": {
        "ru": "Укажите имя ребенка.",
        "kk": "Балаңыздың есімін жазыңыз.",
    },
    "ask_birth_year": {
        "ru": "Укажите год рождения ребенка. Например: *2016*.",
        "kk": "Балаңыздың туған жылын жазыңыз. Мысалы: *2016*.",
    },
    "ask_experience": {
        "ru": "Выберите уровень подготовки:\n1. Начинающий\n2. Средний\n3. Продвинутый",
        "kk": "Дайындық деңгейін таңдаңыз:\n1. Бастауыш\n2. Орта\n3. Жетілдірілген",
    },
    "ask_school_shift": {
        "ru": "Какая школьная смена у ребенка?\n1. Утренняя\n2. Дневная",
        "kk": "Балаңыз қай ауысымда оқиды?\n1. Таңғы\n2. Түскі",
    },
    "no_groups": {
        "ru": "Подходящих групп сейчас не нашлось. Администратор поможет подобрать вариант вручную.",
        "kk": "Қазір сәйкес топ табылмады. Әкімші қолмен нұсқа таңдап береді.",
    },
    "preferred_unavailable": {
        "ru": "Указанное время не подходит для группы ребенка. Выберите один из доступных вариантов:",
        "kk": "Көрсетілген уақыт балаңызға сәйкес топқа келмейді. Қолжетімді нұсқалардың бірін таңдаңыз:",
    },
    "fallback_offer": {
        "ru": "Подходящей группы уровня {requested} сейчас нет.\nЕсть группа уровнем ниже — {offered}, по возрасту и времени подходит.\nЗаписать на пробное туда?",
        "kk": "Қазір {requested} деңгейіне сәйкес топ жоқ.\nБір деңгей төмен {offered} тобы бар, жасы мен уақыты сәйкес.\nСынақ сабағына сол топқа жазайын ба?",
    },
    "fallback_declined": {
        "ru": "Понял. Администратор поможет подобрать подходящую группу вручную.",
        "kk": "Түсіндім. Әкімші сәйкес топты қолмен таңдауға көмектеседі.",
    },
    "choose_slot": {
        "ru": "Выберите время пробного занятия:",
        "kk": "Сынақ сабағының уақытын таңдаңыз:",
    },
    "slot_invalid": {
        "ru": "Введите номер из списка доступных вариантов.",
        "kk": "Қолжетімді нұсқалар тізімінен нөмірді енгізіңіз.",
    },
    "confirm": {
        "ru": "📋 Детали записи:\n📅 {date}\n⏰ {start}–{end}\n👤 Имя ребенка: {name}\n📆 Год рождения: {birth_year}\n🎯 Опыт: {experience}\n🏫 Смена: {school_shift}\n\nПодтвердить?",
        "kk": "📋 Жазылым деректері:\n📅 {date}\n⏰ {start}–{end}\n👤 Балаңыздың есімі: {name}\n📆 Туған жылы: {birth_year}\n🎯 Тәжірибе: {experience}\n🏫 Ауысым: {school_shift}\n\nРастайсыз ба?",
    },
    "confirmed": {
        "ru": "Вы записаны на пробный урок, будем вас ждать!\n📅 {date}\n⏰ {start}–{end}\n👤 Имя ребенка: {name}",
        "kk": "Жазылым сәтті аяқталды, сізді күтеміз!\n📅 {date}\n⏰ {start}–{end}\n👤 Балаңыздың есімі: {name}",
    },
    "declined": {
        "ru": "Запись отменена. Если захотите снова, просто напишите.",
        "kk": "Жазылым тоқтатылды. Қайта қаласаңыз, жазыңыз.",
    },
    "reached_limits": {
        "ru": "Вы достигли максимума пробных занятий.",
        "kk": "Сіз сынақ сабақтарының шегіне жеттіңіз.",
    },
    "has_active_trial": {
        "ru": "У вас уже есть активная запись на пробное занятие.",
        "kk": "Сізде сынақ сабағына белсенді жазылым бар.",
    },
}

WEEKDAY = {
    "ru": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
    "kk": ["Дс", "Сс", "Ср", "Бс", "Жм", "Сб", "Жс"],
}

_EXPERIENCE_ALIASES = {
    "1": "Beginner",
    "beginner": "Beginner",
    "новичок": "Beginner",
    "начальный": "Beginner",
    "бастапқы": "Beginner",
    "2": "Intermediate",
    "intermediate": "Intermediate",
    "средний": "Intermediate",
    "орта": "Intermediate",
    "3": "Advanced",
    "advanced": "Advanced",
    "продвинутый": "Advanced",
    "жоғары": "Advanced",
}
LEVEL_LABELS = {
    "ru": {
        "Beginner": "Начальный",
        "Intermediate": "Средний",
        "Advanced": "Продвинутый",
    },
    "kk": {
        "Beginner": "Бастапқы",
        "Intermediate": "Орта",
        "Advanced": "Жоғары",
    },
}

_SHIFT_ALIASES = {
    "1": "morning",
    "morning": "morning",
    "утренняя": "morning",
    "утром": "morning",
    "таңғы": "morning",
    "2": "afternoon",
    "afternoon": "afternoon",
    "дневная": "afternoon",
    "днем": "afternoon",
    "түскі": "afternoon",
}

_YES = {"да", "иә", "ok", "ок", "подтверждаю", "yes", "жарайды", "дұрыс", "растаймын"}
_NO = {"нет", "жоқ", "no", "отмена", "бас тартамын", "бас тарту"}
_GREETINGS = {
    "hello", "hi", "hey", "привет", "здравствуйте", "салам", "сәлем",
    "сәлеметсіз бе", "добрый день", "доброе утро", "добрый вечер",
    "че там", "чё там", "как дела", "как ты", "что нового",
}
_ACKNOWLEDGEMENTS = {
    "понял", "поняла", "понятно", "ясно", "ок", "окей", "okay", "хорошо",
    "ладно", "спасибо", "спс", "рахмет", "түсіндім", "жақсы",
}
_BOT_IDENTITY_QUESTIONS = (
    "что это за бот", "кто ты", "ты кто", "что ты умеешь",
    "какой это бот", "для чего этот бот", "зачем этот бот",
    "бұл қандай бот", "сен кімсің", "не істей аласың",
)


def _loc(lang: str, key: str, **fmt) -> str:
    text = T[key].get(lang) or T[key]["ru"]
    return text.format(**fmt) if fmt else text


def _fmt_date(value, lang: str) -> str:
    if isinstance(value, date):
        d = value
    else:
        d = datetime.strptime(str(value), "%Y-%m-%d").date()
    return f"{WEEKDAY[lang][d.weekday()]} {d.strftime('%d.%m.%Y')}"


def _normalize_manual_value(field: str, text: str):
    low = text.strip().lower()
    if field == "experience":
        return _EXPERIENCE_ALIASES.get(low)
    if field == "school_shift":
        return _SHIFT_ALIASES.get(low)
    if field == "child_birth_year":
        years = [int(part) for part in low.replace(",", " ").split() if part.isdigit() and len(part) == 4]
        return years[-1] if years else None
    if field == "child_name":
        if not _is_plausible_child_name(text):
            return None
        return text.strip() or None
    return None


def _is_plausible_child_name(text: str | None) -> bool:
    if not text:
        return False
    value = text.strip()
    if not value:
        return False
    if is_bot_identity_question(value) or is_greeting(value) or is_acknowledgement(value):
        return False
    lowered = value.lower()
    blocked_fragments = (
        "что это", "какой это", "кто ты", "что ты", "хочу", "запис",
        "пробн", "занят", "распис", "бот", "можно", "сколько",
    )
    if any(fragment in lowered for fragment in blocked_fragments):
        return False
    if "?" in value or len(value.split()) > 4:
        return False
    return True


def is_greeting(text: str) -> bool:
    normalized = " ".join((text or "").lower().replace("!", " ").replace(".", " ").split())
    return normalized in _GREETINGS


def is_acknowledgement(text: str) -> bool:
    normalized = " ".join((text or "").lower().replace("!", " ").replace(".", " ").split())
    return normalized in _ACKNOWLEDGEMENTS


def is_bot_identity_question(text: str) -> bool:
    normalized = " ".join((text or "").lower().replace("?", " ").replace("!", " ").replace(".", " ").split())
    return any(q in normalized for q in _BOT_IDENTITY_QUESTIONS)


def _waiting_prompt(lang: str, waiting_for: str | None) -> str:
    if not waiting_for:
        return ""
    prompt = _ask_missing(lang, waiting_for)
    if lang == "ru":
        return f"\n\nЧтобы продолжить запись на пробное занятие: {prompt}"
    return f"\n\nСынақ сабағына жазылуды жалғастыру үшін: {prompt}"


def _session_interrupt_response(lang: str, text: str, waiting_for: str | None = None) -> str | None:
    if is_bot_identity_question(text):
        base = (
            "Я бот-ассистент академии. Могу ответить на вопросы о тренировках "
            "и помочь записаться на пробное занятие."
            if lang == "ru"
            else "Мен академияның бот-ассистентімін. Жаттығулар туралы сұрақтарға "
                 "жауап беріп, сынақ сабағына жазуға көмектесемін."
        )
        return base + _waiting_prompt(lang, waiting_for)

    if is_greeting(text):
        base = "Здравствуйте!" if lang == "ru" else "Сәлеметсіз бе!"
        return base + _waiting_prompt(lang, waiting_for)

    if is_acknowledgement(text):
        base = "Хорошо." if lang == "ru" else "Жақсы."
        return base + _waiting_prompt(lang, waiting_for)

    return None


def _draft_to_data(draft: dict) -> dict:
    return {
        "child_name": draft.get("child_name") if _is_plausible_child_name(draft.get("child_name")) else None,
        "child_birth_year": draft.get("child_birth_year"),
        "experience": draft.get("experience"),
        "school_shift": draft.get("school_shift"),
        "preferred_date": draft.get("preferred_date"),
        "preferred_weekday": draft.get("preferred_weekday"),
        "preferred_time_start": draft.get("preferred_time_start"),
        "preferred_time_end": draft.get("preferred_time_end"),
    }


def _merge(base: dict, patch: dict) -> dict:
    merged = dict(base)
    for key, value in patch.items():
        if value not in (None, ""):
            merged[key] = value
    return merged


def _missing_prerequisite(data: dict) -> str | None:
    for key in ("child_name", "child_birth_year", "experience", "school_shift"):
        if data.get(key) in (None, ""):
            return key
    return None


def _ask_missing(lang: str, field: str) -> str:
    return _loc(lang, {
        "child_name": "ask_name",
        "child_birth_year": "ask_birth_year",
        "experience": "ask_experience",
        "school_shift": "ask_school_shift",
    }[field])


def _slot_lines(slots: list[dict], lang: str) -> str:
    lines = []
    for i, slot in enumerate(slots, 1):
        group = slot.get("group_name") or f"Group #{slot.get('group_id')}"
        levels = slot.get("level") or []
        if isinstance(levels, str):
            levels = [levels]
        labels = LEVEL_LABELS.get(lang, LEVEL_LABELS["ru"])
        localized_levels = [labels.get(level, level) for level in levels]
        level_text = f" | {', '.join(localized_levels)}" if localized_levels else ""
        lines.append(
            f"{i}. {group}{level_text}\n"
            f"   {_fmt_date(slot['date'], lang)} | "
            f"{str(slot['time_start'])[:5]}–{str(slot['time_end'])[:5]}"
        )
    return "\n".join(lines)


def _confirmation(draft: dict, lang: str) -> str:
    body = _loc(
        lang,
        "confirm",
        date=_fmt_date(draft["trial_day"], lang),
        start=str(draft["start_time"])[:5],
        end=str(draft["end_time"])[:5],
        name=draft.get("child_name", ""),
        birth_year=draft.get("child_birth_year", ""),
        experience=draft.get("experience", ""),
        school_shift=draft.get("school_shift", ""),
    )
    return body + ("\n\nОтветьте *да* или *нет*." if lang == "ru" else "\n\n*Иә* немесе *жоқ* деп жауап беріңіз.")


class LlmTrialFlowHandler:
    def handle(
        self,
        chat_id: str,
        sender_phone: str,
        bot_name: str,
        user_text: str,
        history: list,
        lang: str,
    ) -> str:
        draft = academy_repo.get_existing_trial_draft(sender_phone, bot_name)
        if not draft:
            result = trial_service.create_or_get_draft(bot_name, chat_id, sender_phone, lang)
            if not result["ok"]:
                if result["code"] == "LIMIT_REACHED":
                    return _loc(lang, "reached_limits")
                if result["code"] == "HAS_ACTIVE_TRIAL":
                    return _loc(lang, "has_active_trial")
                return result.get("message") or _loc(lang, "no_groups")
            draft = result["data"]["trial"]

        if draft.get("child_name") and not _is_plausible_child_name(draft.get("child_name")):
            result = trial_service.update_intake(bot_name, draft["id"], {"child_name": None, "language": lang})
            if not result["ok"]:
                return result.get("message") or _loc(lang, "no_groups")
            draft = result["data"]["trial"]

        active = postgres.get_active_session(bot_name, chat_id)
        waiting_for = (active or {}).get("params", {}).get("waiting_for")
        interrupt = _session_interrupt_response(lang, user_text, waiting_for)
        if waiting_for and interrupt:
            return interrupt

        extracted = extract_trial_details(history, user_text)
        if extracted.get("child_name") and not _is_plausible_child_name(extracted.get("child_name")):
            extracted["child_name"] = None
        if waiting_for and not extracted.get(waiting_for):
            extracted[waiting_for] = _normalize_manual_value(waiting_for, user_text)

        data = _merge(_draft_to_data(draft), extracted)
        result = trial_service.update_intake(bot_name, draft["id"], {**data, "language": lang})
        if not result["ok"]:
            return result.get("message") or _loc(lang, "no_groups")
        draft = result["data"]["trial"]

        missing = _missing_prerequisite(draft)
        if missing:
            postgres.upsert_session(
                bot_name,
                chat_id,
                "trial_intake",
                {"trial_id": draft["id"], "waiting_for": missing, "lang": lang},
                draft["id"],
            )
            return _ask_missing(lang, missing)

        return self._evaluate_slots(chat_id, bot_name, draft, lang)

    def _evaluate_slots(self, chat_id: str, bot_name: str, draft: dict, lang: str) -> str:
        slots = trial_logic.get_eligible_trial_slots(
            bot_name,
            int(draft["child_birth_year"]),
            draft["school_shift"],
            experience=draft.get("experience"),
        )
        if not slots:
            fallback_slots = trial_logic.get_fallback_trial_slots(
                bot_name,
                int(draft["child_birth_year"]),
                draft["school_shift"],
                draft["experience"],
            )
            if fallback_slots:
                slot_levels = fallback_slots[0].get("level") or []
                offered = next((level for level in ("Intermediate", "Beginner") if level in slot_levels), "lower")
                postgres.upsert_session(
                    bot_name,
                    chat_id,
                    "trial_fallback_offer",
                    {
                        "trial_id": draft["id"],
                        "lang": lang,
                        "requested_level": draft["experience"],
                        "offered_level": offered,
                        "slots": [
                            {
                                "group_id": s["group_id"],
                                "date": str(s["date"]),
                                "time_start": str(s["time_start"])[:5],
                                "time_end": str(s["time_end"])[:5],
                                "group_name": s.get("group_name"),
                                "level": s.get("level") or [],
                                "field": s.get("field"),
                            }
                            for s in fallback_slots
                        ],
                    },
                    draft["id"],
                )
                return _loc(
                    lang, "fallback_offer",
                    requested=draft["experience"], offered=offered,
                )
            postgres.delete_session(bot_name, chat_id)
            return _loc(lang, "no_groups")

        postgres.upsert_session(
            bot_name,
            chat_id,
            "trial_select_slot",
            {
                "trial_id": draft["id"],
                "slots": [
                    {
                        "group_id": s["group_id"],
                        "date": str(s["date"]),
                        "time_start": str(s["time_start"])[:5],
                        "time_end": str(s["time_end"])[:5],
                        "group_name": s.get("group_name"),
                        "level": s.get("level") or [],
                        "field": s.get("field"),
                    }
                    for s in slots
                ],
                "lang": lang,
            },
            draft["id"],
        )
        return f"{_loc(lang, 'choose_slot')}\n\n{_slot_lines(slots, lang)}"

    def _assign_slot_and_confirm(
        self, chat_id: str, bot_name: str, draft: dict, slot: dict, lang: str
    ) -> str:
        result = trial_service.assign_slot(bot_name, chat_id, draft["id"], slot, lang)
        if not result["ok"]:
            return result.get("message") or _loc(lang, "no_groups")
        draft = result["data"]["trial"]
        return _confirmation(draft, lang)

    def handle_session_turn(
        self,
        chat_id: str,
        sender_phone: str,
        bot_name: str,
        user_text: str,
        history: list,
        session: dict,
    ) -> str | None:
        state = session["state"]
        params = session["params"]
        lang = params.get("lang", "ru")
        trial_id = params.get("trial_id")

        if state == "trial_intake":
            interrupt = _session_interrupt_response(lang, user_text, params.get("waiting_for"))
            if interrupt:
                return interrupt
            return self.handle(chat_id, sender_phone, bot_name, user_text, history, lang)

        if state == "trial_select_slot":
            interrupt = _session_interrupt_response(lang, user_text)
            if interrupt:
                return interrupt + "\n\n" + f"{_loc(lang, 'choose_slot')}\n\n{_slot_lines(params.get('slots') or [], lang)}"
            if not user_text.strip().isdigit():
                return _loc(lang, "slot_invalid")
            idx = int(user_text.strip()) - 1
            slots = params.get("slots") or []
            if idx < 0 or idx >= len(slots):
                return _loc(lang, "slot_invalid")
            draft = academy_repo.get_trial(trial_id)
            return self._assign_slot_and_confirm(chat_id, bot_name, draft, slots[idx], lang)

        if state == "trial_fallback_offer":
            lower = user_text.lower().strip()
            decision = "yes" if any(w in lower for w in _YES) else "no" if any(w in lower for w in _NO) else ""
            slots = params.get("slots") or []
            if decision == "yes":
                postgres.upsert_session(
                    bot_name,
                    chat_id,
                    "trial_select_slot",
                    {"trial_id": trial_id, "slots": slots, "lang": lang},
                    trial_id,
                )
                return f"{_loc(lang, 'choose_slot')}\n\n{_slot_lines(slots, lang)}"
            if decision == "no":
                postgres.delete_session(bot_name, chat_id)
                return _loc(lang, "fallback_declined")
            return _loc(
                lang, "fallback_offer",
                requested=params.get("requested_level", ""),
                offered=params.get("offered_level", ""),
            )

        if state == "trial_confirm":
            lower = user_text.lower().strip()
            decision = "yes" if any(w in lower for w in _YES) else "no" if any(w in lower for w in _NO) else ""
            if decision == "yes":
                result = trial_service.confirm_trial(bot_name, chat_id, trial_id)
                if not result["ok"]:
                    return result.get("message") or _confirmation(academy_repo.get_trial(trial_id), lang)
                trial = result["data"]["trial"]
                return _loc(
                    lang,
                    "confirmed",
                    date=_fmt_date(trial["trial_day"], lang),
                    start=str(trial["start_time"])[:5],
                    end=str(trial["end_time"])[:5],
                    name=trial.get("child_name", ""),
                )
            if decision == "no":
                trial_service.cancel_trial(
                    bot_name, chat_id, trial_id, "user_declined_gated_trial"
                )
                return _loc(lang, "declined")
            return _confirmation(academy_repo.get_trial(trial_id), lang)

        return None
