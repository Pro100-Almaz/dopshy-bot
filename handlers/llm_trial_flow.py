import logging
import uuid
from datetime import date, datetime

from chat.conversation import clear_history
from handlers.academy_extractor import extract_trial_details
from integrations import trial as trial_logic
from integrations.repo import academy_repo, postgres
from integrations.sheets.trial_sheets import upsert_trial_row

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
        "ru": "Выберите уровень подготовки:\n1. Beginner\n2. Intermediate\n3. Advanced",
        "kk": "Дайындық деңгейін таңдаңыз:\n1. Beginner\n2. Intermediate\n3. Advanced",
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
        return text.strip() or None
    return None


def _draft_to_data(draft: dict) -> dict:
    return {
        "child_name": draft.get("child_name"),
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
        lines.append(
            f"{i}. {_fmt_date(slot['date'], lang)} "
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
            if not academy_repo.check_trial_limits(bot_name, sender_phone):
                return _loc(lang, "reached_limits")
            if academy_repo.has_active_trial(bot_name, sender_phone):
                return _loc(lang, "has_active_trial")
            result = postgres.create_draft(
                bot_name,
                chat_id=chat_id,
                phone=sender_phone,
                client_token=str(uuid.uuid4()),
                language=lang,
            )
            draft = academy_repo.get_trial(result["data"]["trial_id"])

        extracted = extract_trial_details(history, user_text)
        active = postgres.get_active_session(bot_name, chat_id)
        waiting_for = (active or {}).get("params", {}).get("waiting_for")
        if waiting_for and not extracted.get(waiting_for):
            extracted[waiting_for] = _normalize_manual_value(waiting_for, user_text)

        data = _merge(_draft_to_data(draft), extracted)
        postgres.update_draft(bot_name, draft["id"], **data, language=lang)
        draft = academy_repo.get_trial(draft["id"])

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
                offered = fallback_slots[0].get("level") or "lower"
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
                                "level": s.get("level"),
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

        matched = trial_logic.match_preferred_slot(slots, draft)
        if matched:
            return self._assign_slot_and_confirm(chat_id, bot_name, draft, matched, lang)

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
                    }
                    for s in slots
                ],
                "lang": lang,
            },
            draft["id"],
        )
        prefix = "preferred_unavailable" if any(draft.get(k) for k in (
            "preferred_date", "preferred_weekday", "preferred_time_start", "preferred_time_end"
        )) else "choose_slot"
        return f"{_loc(lang, prefix)}\n\n{_slot_lines(slots, lang)}"

    def _assign_slot_and_confirm(
        self, chat_id: str, bot_name: str, draft: dict, slot: dict, lang: str
    ) -> str:
        postgres.update_draft(
            bot_name,
            draft["id"],
            trial_day=str(slot["date"]),
            start_time=str(slot["time_start"])[:5],
            end_time=str(slot["time_end"])[:5],
            group_id=slot["group_id"],
        )
        draft = academy_repo.get_trial(draft["id"])
        postgres.upsert_session(
            bot_name,
            chat_id,
            "trial_confirm",
            {"trial_id": draft["id"], "lang": lang},
            draft["id"],
        )
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
            return self.handle(chat_id, sender_phone, bot_name, user_text, history, lang)

        if state == "trial_select_slot":
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
                academy_repo.confirm_trial(trial_id)
                confirmed = academy_repo.get_trial_with_user_by_id(trial_id)
                if confirmed:
                    upsert_trial_row(confirmed)
                postgres.delete_session(bot_name, chat_id)
                clear_history(chat_id)
                trial = academy_repo.get_trial(trial_id)
                return _loc(
                    lang,
                    "confirmed",
                    date=_fmt_date(trial["trial_day"], lang),
                    start=str(trial["start_time"])[:5],
                    end=str(trial["end_time"])[:5],
                    name=trial.get("child_name", ""),
                )
            if decision == "no":
                postgres.cancel_booking_trial(
                    bot_name,
                    trial_id,
                    actor_type="chatbot:Бот",
                    actor_id=chat_id,
                    reason="user_declined_gated_trial",
                )
                postgres.delete_session(bot_name, chat_id)
                clear_history(chat_id)
                return _loc(lang, "declined")
            return _confirmation(academy_repo.get_trial(trial_id), lang)

        return None
