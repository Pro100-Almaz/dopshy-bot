"""
Non-deterministic LLM-driven trial-signup flow (academy bots).

The arena counterpart is llm_booking_flow.py. Same architecture:
- Accepts signup data in any order (no fixed step sequence)
- Uses the LLM to extract intent + params from natural language
- Creates or continues a draft trial based on whatever data is available
- Identifies a continuation by phone + state='draft' + group_type, so no
  trial_sessions row is needed and out-of-order data cannot break step ordering

What is NOT ported from the arena flow:
- the earlier-start suggestion (BaseChecker._offer_earlier_start and friends) —
  it only makes sense for a free-form slot that can slide leftward
- pricing, payment receipts and the reservation TTL — a trial is free
- field/format resolution — a class is one dimension, not two

Where it necessarily differs: an academy has no free-form slot grid. The parent
picks one of a handful of existing weekly classes, so the arena's seven checking
rules collapse into four, and the class supplies time_end and group_id rather
than the parent supplying them (see _evaluate_and_respond).

Registration data is identical to the deterministic flow (trial_session.py):
date, start/end time, group, child name, child age.
"""

import logging
import uuid

from chat.conversation import clear_history
from integrations import trial as trial_logic
from integrations.repo import academy_repo, postgres
from integrations.sheets.trial_sheets import refresh_all_trials
from utils import is_past_booking_time
import re
from datetime import date, datetime

from handlers.academy_extractor import extract_trial_details
from handlers.sessions.trial_session import _MY_TRIAL_KW
from integrations import trial_service
from integrations import trial as trial_logic
from integrations.repo import academy_repo, postgres
from utils import today_almaty

logger = logging.getLogger(__name__)


T = {
    "ask_name": {
        "ru": "Укажите имя ребенка.",
        "kk": "Балаңыздың есімін жазыңыз.",
    },
    "ask_name_self": {
        "ru": "Укажите ваше имя.",
        "kk": "Атыңызды жазыңыз.",
    },
    "birth_year_invalid": {
        "ru": "Год рождения {year} не подходит — принимаем детей от {min_age} до {max_age} лет. "
              "Укажите год рождения ребенка. Например: *2016*.",
        "kk": "{year} туған жылы сәйкес емес — {min_age} мен {max_age} жас аралығындағы балаларды қабылдаймыз. "
              "Балаңыздың туған жылын жазыңыз. Мысалы: *2016*.",
    },
    "name_too_long": {
        "ru": "Имя слишком длинное — не больше {max_len} символов. Напишите, пожалуйста, короче.",
        "kk": "Есім тым ұзын — {max_len} таңбадан аспауы керек. Қысқарақ жазыңыз.",
    },
    "ask_birth_year": {
        "ru": "Укажите год рождения ребенка. Например: *2016*.",
        "kk": "Балаңыздың туған жылын жазыңыз. Мысалы: *2016*.",
    },
    "ask_experience": {
        "ru": "Выберите уровень подготовки:\n1. Начинающий\n2. Средний\n3. Продвинутый",
        "kk": "Дайындық деңгейін таңдаңыз:\n1. Бастауыш\n2. Орта\n3. Жетілген",
    },
    "ask_school_shift": {
        "ru": "Какая школьная смена у ребенка?\n1. Утренняя\n2. Дневная",
        "kk": "Балаңыз қай ауысымда оқиды?\n1. Таңғы\n2. Түскі",
    },
    "no_groups": {
        "ru": "Подходящих групп сейчас не нашлось. Администратор поможет подобрать вариант вручную.",
        "kk": "Қазір сәйкес топ табылмады. Әкімші қолмен нұсқа таңдап береді.",
    },
    "no_groups_call_admin": {
        "ru": "Подходящих групп сейчас не нашлось. Пожалуйста, позвоните администратору по номеру +7 700 555 6000.",
        "kk": "Қазір сәйкес топ табылмады. Әкімшіге +7 700 555 6000 нөміріне қоңырау шалыңыз.",
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
        "ru": "Выберите группу на {date}:",
        "kk": "{date} күніне топты таңдаңыз:",
    },
    "slot_invalid": {
        "ru": "Введите номер из списка доступных вариантов.",
        "kk": "Қолжетімді нұсқалар тізімінен нөмірді енгізіңіз.",
    },
    "choose_day": {
        "ru": "На какой день хотите записаться на пробное занятие?",
        "kk": "Сынақ сабағына қай күнге жазылғыңыз келеді?",
    },
    "day_invalid": {
        "ru": "Введите номер даты из списка, или напишите день недели.",
        "kk": "Тізімдегі күн нөмірін немесе апта күнін жазыңыз.",
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
        "ru": (
            "По этому номеру уже есть использованная пробная запись. "
            "Если вы считаете, что это ошибка, администратор проверит вручную."
        ),
        "kk": (
            "Бұл нөмір бойынша сынақ жазылымы бұрын қолданылған. "
            "Егер бұл қате деп ойласаңыз, әкімші қолмен тексереді."
        ),
    },
    "has_active_trial": {
        "ru": (
            "По этому номеру уже есть активная запись на пробное занятие. "
            "Если нужна дата или перенос, напишите: «моя запись» или «перенести запись»."
        ),
        "kk": (
            "Бұл нөмір бойынша сынақ сабағына белсенді жазылым бар. "
            "Күні керек болса немесе ауыстырғыңыз келсе: «менің жазылымым» немесе «жазылымды ауыстыру» деп жазыңыз."
        ),
    },
    "details_updated_confirm": {
        "ru": "Данные обновил. Проверьте детали и подтвердите запись.",
        "kk": "Деректер жаңартылды. Мәліметтерді тексеріп, жазылымды растаңыз.",
    },
    "slot_no_longer_eligible": {
        "ru": "После изменения данных выбранная группа уже не подходит. Я убрал выбранные дату и время — выберите подходящий вариант заново.",
        "kk": "Деректер өзгергеннен кейін таңдалған топ сәйкес келмейді. Таңдалған күн мен уақытты алып тастадым — қолайлы нұсқаны қайта таңдаңыз.",
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


def _build_word_re(words: set[str]) -> re.Pattern | None:
    alnum = [re.escape(w) for w in words if any(ch.isalnum() for ch in w)]
    return re.compile(r"\b(?:" + "|".join(alnum) + r")\b", re.IGNORECASE) if alnum else None


_YES_RE = _build_word_re(_YES)
_NO_RE = _build_word_re(_NO)

# Longer stems for "cancel", checked as plain substrings (not word-boundary):
# Russian inflects these (отменить/отменяю/отменил/...), so a whole-word match
# would miss most of them, and the stems are long enough that a false-positive
# substring collision is essentially impossible — unlike short tokens such as
# "да", which is why those go through _YES_RE/_NO_RE above instead.
_CANCEL_STEMS = ("отмен", "откаж", "передумал", "бас тарт")


def _confirm_decision(text: str) -> str | None:
    """Return 'yes'/'no'/None from free text.

    Short confirm words are matched on a word boundary so "да" doesn't fire
    on "дать" (giving a false "yes" to an unrelated question containing the
    word "give"/"дать" was a real production incident). Cancel stems are
    matched as substrings on purpose — see _CANCEL_STEMS.
    """
    lower = text.lower().strip()
    if _YES_RE and _YES_RE.search(lower):
        return "yes"
    if (_NO_RE and _NO_RE.search(lower)) or any(stem in lower for stem in _CANCEL_STEMS):
        return "no"
    return None


def _real_changes(patch: dict, current: dict) -> dict:
    """Drop patch entries that already match the stored value.

    The extractor is fed the whole conversation (needed to resolve relative
    dates/pronouns), so re-mentioning an already-known fact — even while
    trying to cancel — can come back as an "extracted" field though nothing
    actually changed. Only a genuine difference should be treated as an edit.
    """
    return {k: v for k, v in patch.items() if str((current or {}).get(k)) != str(v)}


def _reasoned_fallback(
    bot_name: str, lang: str, user_text: str, reminder: str, weave_hint: str | None = None,
    extra_hint: str | None = None,
) -> str:
    """Answer a message that didn't move the flow forward and wasn't caught by
    any of the specific handlers above (a value, yes/no, or greeting/identity
    interrupt) — an objection, a side question, or anything else.

    Answers briefly using the academy knowledge base (same RAG used outside
    the flow).

    `reminder` is the prompt/summary the caller still needs the user to act
    on. When it's structured content (a slot list, a price/date confirmation
    card) it is appended verbatim — that's factual data an LLM must not be
    left to paraphrase or hallucinate. When `weave_hint` is given instead
    (a plain one-line intake question), the model folds the reminder into
    its own answer, phrased differently each time and skipped or softened
    when forcing it would read as a non-sequitur (e.g. re-asking a child's
    name right after the user asked whether adults can sign up) — a fixed
    reminder glued onto every reply regardless of context read as the bot
    ignoring what was just asked.
    """
    from chat.llm import get_trial_reply
    from rag.retriever import retrieve_context

    try:
        context = retrieve_context(user_text, bot_name=bot_name)
    except Exception:
        logger.exception("[TRIAL] RAG lookup failed in reasoned fallback")
        context = ""

    hint = (
        "Пользователь сейчас в процессе записи на пробное занятие и написал "
        "что-то, что не является ни значением для текущего вопроса, ни "
        "подтверждением/отменой записи. Кратко ответь по существу на его "
        "вопрос или возражение, используя базу знаний, если она относится к "
        "теме; если базы знаний недостаточно, вежливо скажи, что уточнишь у "
        "администратора. Не подтверждай и не отменяй запись сам."
    )
    if weave_hint:
        hint += (
            f" После ответа, в том же сообщении и своими словами (не повторяй "
            f"один и тот же вопрос одинаковой фразой каждый раз), напомни, что "
            f"для продолжения записи на пробное занятие {weave_hint}. Если из "
            f"сообщения пользователя ясно, что речь может идти не о его "
            f"ребёнке (например, вопрос про взрослых или про другое "
            f"направление), не задавай этот вопрос как ни в чём не бывало — "
            f"сначала мягко проясни это, и лишь при уместности напомни про "
            f"запись."
        )
    if extra_hint:
        hint += f" {extra_hint}"
    try:
        answer = get_trial_reply(user_text, context, system_hint=hint)
    except Exception:
        logger.exception("[TRIAL] Reasoned fallback LLM call failed")
        answer = ""
    if weave_hint:
        return answer or reminder
    return f"{answer}\n\n{reminder}".strip() if answer else reminder
_GREETINGS = {
    "hello", "hi", "hey", "привет", "здравствуйте", "салам", "сәлем",
    "сәлеметсіз бе", "добрый день", "доброе утро", "добрый вечер",
    "че там", "чё там", "как дела", "как ты", "что нового",
}
_ACKNOWLEDGEMENTS = {
    "понял", "поняла", "понятно", "ясно", "ок", "окей", "okay", "хорошо",
    "ладно", "спасибо", "спс", "рахмет", "түсіндім", "жақсы",
}
# Stems for "this is a factual question, not a signup request". The intent
# router is tuned to over-trigger trial_new/trial_continue, so an unanswered
# price/schedule/location question used to be swallowed by the booking flow.
# Stems are short so Russian declensions and Kazakh suffixes both match.
_FACTUAL_QUESTION_STEMS = (
    "скольк", "сколко", "цена", "цены", "стоим", "стоит", "тариф", "прайс",
    "расписан", "график", "график заняти", "во сколько", "когда заняти",
    "где наход", "адрес", "локац", "как добрат",
    "с какого возраст", "какой возраст", "во сколько лет",
    "что нужно", "что взять", "что брать",
    "қанша", "баға", "құны", "кесте", "қай уақыт", "қайда", "мекенжай",
    "неше жаст", "не керек",
)

_BOT_IDENTITY_QUESTIONS = (
    "что это за бот", "кто ты", "ты кто", "что ты умеешь",
    "какой это бот", "для чего этот бот", "зачем этот бот",
    "бұл қандай бот", "сен кімсің", "не істей аласың",
)
_AGE_RE = re.compile(
    r"\b(?:мне|маған|жасым)\s+(\d{1,2})\s*(?:лет|жас)?\b"
    r"|\b(\d{1,2})\s*(?:лет|жас)(?:\s+мне|\s+маған)?\b",
    re.IGNORECASE,
)
_SELF_SIGNUP_RE = re.compile(
    r"\b(?:мне|я\s+хочу|хочу\s+записаться|хочу\s+заниматься|маған|өзім)\b",
    re.IGNORECASE,
)


def _loc(lang: str, key: str, **fmt) -> str:
    text = T[key].get(lang) or T[key]["ru"]
    return text.format(**fmt) if fmt else text


def _birth_year_from_age(text: str, today: date | None = None) -> int | None:
    today = today or today_almaty()
    for match in _AGE_RE.finditer(text or ""):
        raw = match.group(1) or match.group(2)
        if not raw:
            continue
        age = int(raw)
        if 5 <= age <= 15:
            return today.year - age
        _log_rejected("child_age", "out_of_range", age, allowed="5-15")
    return None


def _signup_actor(text: str | None) -> str | None:
    return "self" if _SELF_SIGNUP_RE.search(text or "") else None


def _is_my_trial_query(text: str) -> bool:
    """True for "what trial(s) do I have" — reuses trial_session.py's proven
    keyword list rather than a second, drifting copy of the same intent.
    """
    lower = (text or "").lower()
    return any(kw in lower for kw in _MY_TRIAL_KW)


def _extract_user_data(
    history: list,
    user_text: str,
    waiting_for: str | None = None,
) -> dict:
    extracted = extract_trial_details(history, user_text)
    if not extracted.get("child_birth_year"):
        extracted["child_birth_year"] = _birth_year_from_age(user_text)
    _drop_invalid_fields(extracted)
    if not extracted.get("experience"):
        extracted["experience"] = _normalize_manual_value("experience", user_text)
    if not extracted.get("school_shift"):
        extracted["school_shift"] = _normalize_manual_value("school_shift", user_text)
    if waiting_for and not extracted.get(waiting_for):
        extracted[waiting_for] = _normalize_manual_value(waiting_for, user_text)
    if waiting_for == "child_birth_year" and not extracted.get("child_birth_year"):
        extracted["child_birth_year"] = _birth_year_from_age(user_text)
    _drop_invalid_fields(extracted)
    return extracted


def _filled_patch(data: dict) -> dict:
    return {key: value for key, value in (data or {}).items() if value not in (None, "")}


def _match_day_choice(user_text: str, history: list, dates: list) -> object | None:
    """Match free text (a weekday name or an explicit date) against `dates`.

    Weekday-name matching (trial_logic.parse_weekdays) is checked first —
    it's local, free, and covers the expected phrasing ("на среду"). Only
    falls back to the LLM extractor's preferred_date for an explicit date
    ("24 сентября") if no weekday was named.
    """
    weekdays = trial_logic.parse_weekdays(user_text)
    if weekdays:
        for d in dates:
            d_date = d if isinstance(d, date) else datetime.strptime(str(d), "%Y-%m-%d").date()
            if d_date.weekday() in weekdays:
                return d
    preferred = _extract_user_data(history, user_text).get("preferred_date")
    if preferred:
        for d in dates:
            if str(d) == str(preferred):
                return d
    return None


def _fmt_date(value, lang: str) -> str:
    if isinstance(value, date):
        d = value
    else:
        d = datetime.strptime(str(value), "%Y-%m-%d").date()
    return f"{WEEKDAY[lang][d.weekday()]} {d.strftime('%d.%m.%Y')}"


def _normalize_manual_value(field: str, text: str):
    low = " ".join((text or "").strip().lower().replace("?", " ").replace("!", " ").split())
    if field == "experience":
        exact = _EXPERIENCE_ALIASES.get(low)
        if exact:
            return exact
        if "начина" in low or "нович" in low or "бастап" in low:
            return "Beginner"
        if "средн" in low or "орта" in low:
            return "Intermediate"
        if "продвин" in low or "жоғары" in low:
            return "Advanced"
        return None
    if field == "school_shift":
        exact = _SHIFT_ALIASES.get(low)
        if exact:
            return exact
        if "утрен" in low or "утром" in low or "таң" in low:
            return "morning"
        if "дневн" in low or "днем" in low or "түск" in low:
            return "afternoon"
        return None
    if field == "child_birth_year":
        years = [int(part) for part in low.replace(",", " ").split() if part.isdigit() and len(part) == 4]
        return years[-1] if years else None
    if field == "child_name":
        if not _is_plausible_child_name(text):
            return None
        return text.strip() or None
    return None


# academy_trials.child_name is VARCHAR(30); a longer value fails the UPDATE.
CHILD_NAME_MAX_LEN = trial_service.TRIAL_TEXT_LIMITS["child_name"]
# Birth years outside this age window cannot be a child for any academy group.
CHILD_MIN_AGE = 5
CHILD_MAX_AGE = 18


def _log_rejected(field: str, reason: str, value, **context) -> None:
    """One log format for every intake value the flow refuses to store."""
    extra = "".join(f" {k}={v}" for k, v in context.items())
    logger.info("[TRIAL] %s rejected: reason=%s%s value=%r", field, reason, extra, value)


def _birth_year_rejection(value, today: date | None = None) -> str | None:
    """Return why `value` is not a usable child birth year, or None if it is."""
    try:
        year = int(value)
    except (TypeError, ValueError):
        return "not_a_year"
    current = (today or today_almaty()).year
    if year > current:
        return "in_future"
    if current - year < CHILD_MIN_AGE:
        return "too_young"
    if current - year > CHILD_MAX_AGE:
        return "too_old"
    return None


def _drop_invalid_fields(extracted: dict) -> dict[str, tuple[str, object]]:
    """Null out extracted values that break a restriction.

    Returns ``{field: (reason, rejected_value)}`` so the caller can tell the
    client why the field is asked again.
    """
    rejected = {}
    name = extracted.get("child_name")
    if name:
        reason = _child_name_rejection(name)
        if reason:
            _log_rejected("child_name", reason, name,
                          len=len(name.strip()), max_len=CHILD_NAME_MAX_LEN)
            rejected["child_name"] = (reason, name)
            extracted["child_name"] = None
    year = extracted.get("child_birth_year")
    if year not in (None, ""):
        reason = _birth_year_rejection(year)
        if reason:
            _log_rejected("child_birth_year", reason, year,
                          allowed_age=f"{CHILD_MIN_AGE}-{CHILD_MAX_AGE}")
            rejected["child_birth_year"] = (reason, year)
            extracted["child_birth_year"] = None
    return rejected


def _child_name_rejection(text: str | None) -> str | None:
    """Return why `text` cannot be stored as a child name, or None if it can."""
    if not text:
        return "empty"
    value = text.strip()
    if not value:
        return "empty"
    if is_bot_identity_question(value) or is_greeting(value) or is_acknowledgement(value):
        return "greeting"
    lowered = value.lower()
    blocked_fragments = (
        "что это", "какой это", "кто ты", "что ты", "хочу", "запис",
        "пробн", "занят", "распис", "бот", "можно", "сколько",
    )
    if any(fragment in lowered for fragment in blocked_fragments):
        return "blocked_phrase"
    if "?" in value:
        return "question"
    if len(value.split()) > 4:
        return "too_many_words"
    if len(value) > CHILD_NAME_MAX_LEN:
        return "too_long"
    return None


def _is_plausible_child_name(text: str | None) -> bool:
    reason = _child_name_rejection(text)
    if reason and reason != "empty":
        _log_rejected("child_name", reason, text,
                      len=len(text.strip()), max_len=CHILD_NAME_MAX_LEN)
    return reason is None


def is_greeting(text: str) -> bool:
    normalized = " ".join((text or "").lower().replace("!", " ").replace(".", " ").split())
    return normalized in _GREETINGS


def is_acknowledgement(text: str) -> bool:
    normalized = " ".join((text or "").lower().replace("!", " ").replace(".", " ").split())
    return normalized in _ACKNOWLEDGEMENTS


def is_bot_identity_question(text: str) -> bool:
    normalized = " ".join((text or "").lower().replace("?", " ").replace("!", " ").replace(".", " ").split())
    return any(q in normalized for q in _BOT_IDENTITY_QUESTIONS)


def is_factual_question(text: str) -> bool:
    """True for price/schedule/location/age questions that deserve a real answer.

    Used to veto the intent router when it classifies such a question as a
    signup: the booking flow cannot answer it, so it must fall through to
    RAG/LLM instead.
    """
    normalized = " ".join((text or "").lower().replace("?", " ").replace("!", " ").replace(".", " ").split())
    return any(stem in normalized for stem in _FACTUAL_QUESTION_STEMS)


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


# What each intake field means, for _reasoned_fallback's weave_hint — a
# semantic description the model rephrases each time, not the literal
# question (that's _ask_missing, used only as the hard-fallback reminder).
_FIELD_WEAVE_HINTS = {
    "child_name": "нужно узнать имя ребёнка",
    "child_birth_year": "нужно узнать год рождения ребёнка",
    "experience": "нужно узнать уровень подготовки ребёнка (начальный, средний или продвинутый)",
    "school_shift": "нужно узнать школьную смену ребёнка (утренняя или дневная)",
}


def _ask_missing(lang: str, field: str, signup_actor: str | None = None) -> str:
    if field == "child_name" and signup_actor == "self":
        return _loc(lang, "ask_name_self")
    return _loc(lang, {
        "child_name": "ask_name",
        "child_birth_year": "ask_birth_year",
        "experience": "ask_experience",
        "school_shift": "ask_school_shift",
    }[field])


def _level_label(value: str | None, lang: str) -> str:
    return LEVEL_LABELS.get(lang, LEVEL_LABELS["ru"]).get(value, value or "")


def _shift_label(value: str | None, lang: str) -> str:
    return SHIFT_LABELS.get(lang, SHIFT_LABELS["ru"]).get(value, value or "")


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


def _serialize_slots(slots: list[dict]) -> list[dict]:
    """Session params are JSON — collapse each slot to plain str/primitive fields."""
    return [
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
    ]


def _day_lines(dates: list, lang: str) -> str:
    return "\n".join(f"{i}. {_fmt_date(d, lang)}" for i, d in enumerate(dates, 1))


def _group_lines_for_day(slots: list[dict], lang: str) -> str:
    """Like _slot_lines, but the date is already fixed and known — every slot
    here landed on the same day the user just picked, so repeating it on
    every line would be pure clutter.
    """
    lines = []
    labels = LEVEL_LABELS.get(lang, LEVEL_LABELS["ru"])
    for i, slot in enumerate(slots, 1):
        group = slot.get("group_name") or f"Group #{slot.get('group_id')}"
        levels = slot.get("level") or []
        if isinstance(levels, str):
            levels = [levels]
        localized_levels = [labels.get(level, level) for level in levels]
        level_text = f" | {', '.join(localized_levels)}" if localized_levels else ""
        lines.append(
            f"{i}. {group} {str(slot['time_start'])[:5]}–{str(slot['time_end'])[:5]}{level_text}"
        )
    return "\n".join(lines)


def _age_group_lines(slots: list[dict], lang: str) -> str:
    seen = set()
    unique = []
    for slot in slots:
        key = (
            slot.get("group_id"),
            slot.get("training_day"),
            str(slot.get("time_start"))[:5],
            str(slot.get("time_end"))[:5],
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(slot)
    return _slot_lines(unique, lang)


def _no_groups_with_age_options(
    lang: str,
    slots: list[dict],
    draft: dict,
    signup_actor: str | None = None,
) -> str:
    if not slots:
        return _loc(lang, "no_groups_call_admin")
    if lang == "ru":
        return (
            f"{_loc(lang, 'no_groups')}\n\n"
            f"По возрасту {draft.get('child_birth_year')} подходят такие группы:\n"
            f"{_age_group_lines(slots, lang)}\n\n"
            "Можно поменять уровень подготовки или школьную смену, и я проверю ещё раз."
        )
    return (
        f"{_loc(lang, 'no_groups')}\n\n"
        f"{draft.get('child_birth_year')} туған жылына сәйкес келетін топтар:\n"
        f"{_age_group_lines(slots, lang)}\n\n"
        "Дайындық деңгейін немесе мектеп ауысымын өзгертсеңіз, қайта тексеремін."
    )


def _confirmation(draft: dict, lang: str) -> str:
    body = _loc(
        lang,
        "confirm",
        date=_fmt_date(draft["trial_day"], lang),
        start=str(draft["start_time"])[:5],
        end=str(draft["end_time"])[:5],
        name=draft.get("child_name", ""),
        birth_year=draft.get("child_birth_year", ""),
        experience=_level_label(draft.get("experience"), lang),
        school_shift=_shift_label(draft.get("school_shift"), lang),
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
        # "какие у меня пробные" / "мои занятия" etc. — checked before touching
        # any draft state, so asking about an existing signup never creates an
        # unwanted empty one. Works both as a fresh message and mid-intake
        # (handle_session_turn's trial_intake state re-enters here).
        if _is_my_trial_query(user_text):
            from handlers.edit_trial import handle_trial_status_request
            active = postgres.get_active_session(bot_name, chat_id)
            waiting_for = (active or {}).get("params", {}).get("waiting_for")
            status = handle_trial_status_request(sender_phone, bot_name, lang)
            return f"{status}{_waiting_prompt(lang, waiting_for)}" if waiting_for else status

        draft = academy_repo.get_existing_trial_draft(sender_phone, bot_name)
        if not draft:
            result = trial_service.create_or_get_draft(bot_name, chat_id, sender_phone, lang)
            if not result["ok"]:
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
        signup_actor = (active or {}).get("params", {}).get("signup_actor") or _signup_actor(user_text)
        interrupt = _session_interrupt_response(lang, user_text, waiting_for)
        if waiting_for and interrupt:
            return interrupt

        extracted = extract_trial_details(history, user_text)
        rejected = _drop_invalid_fields(extracted)
        if waiting_for and not extracted.get(waiting_for) and waiting_for not in rejected:
            manual = _normalize_manual_value(waiting_for, user_text)
            if manual is not None and waiting_for == "child_birth_year":
                reason = _birth_year_rejection(manual)
                if reason:
                    _log_rejected(waiting_for, reason, manual,
                                  allowed_age=f"{CHILD_MIN_AGE}-{CHILD_MAX_AGE}")
                    rejected[waiting_for] = (reason, manual)
                    manual = None
            elif manual is None and waiting_for == "child_name":
                # _is_plausible_child_name has already logged the reason.
                rejected[waiting_for] = (_child_name_rejection(user_text) or "empty", user_text)
            elif manual is None:
                _log_rejected(waiting_for, "unrecognized", user_text)
                rejected[waiting_for] = ("unrecognized", user_text)
            extracted[waiting_for] = manual

        data = _merge(_draft_to_data(draft), extracted)
        result = trial_service.update_intake(bot_name, draft["id"], {**data, "language": lang})
        if not result["ok"]:
            return result.get("message") or _loc(lang, "no_groups")
        draft = result["data"]["trial"]

        reply = self._continue_from_draft(chat_id, bot_name, draft, lang, signup_actor)
        # _continue_from_draft has already re-armed the session to wait for the
        # missing field; only the prompt changes, so the client knows why it is
        # asked again.
        missing = _missing_prerequisite(_draft_to_data(draft))
        reason, bad_value = rejected.get(missing, (None, None))
        if missing == "child_name" and reason == "too_long":
            logger.info("[TRIAL] Asking chat_id=%s for a shorter child_name (max %d chars)",
                        chat_id, CHILD_NAME_MAX_LEN)
            return _loc(lang, "name_too_long", max_len=CHILD_NAME_MAX_LEN)
        # Only a genuinely parseable-but-out-of-range year gets this specific
        # message. "unrecognized"/"not_a_year" mean there was no year-like
        # token in the message at all (e.g. "мои занятия", "что ты несешь?")
        # — that's not a rejected value, it's a non-answer, and belongs in
        # the reasoned fallback below, not in a message that echoes it back
        # as if it were a year.
        if missing == "child_birth_year" and reason in ("in_future", "too_young", "too_old"):
            logger.info("[TRIAL] Asking chat_id=%s for a valid child_birth_year (age %d-%d)",
                        chat_id, CHILD_MIN_AGE, CHILD_MAX_AGE)
            return _loc(lang, "birth_year_invalid", year=bad_value,
                        min_age=CHILD_MIN_AGE, max_age=CHILD_MAX_AGE)
        # Stuck on the exact same field we were waiting for, and the message
        # wasn't a usable value for it (not caught by either message above) —
        # it's an objection, a side question, or something else entirely.
        # Answer it instead of silently repeating the identical prompt.
        if waiting_for and missing == waiting_for and missing in rejected:
            logger.info("[TRIAL] chat_id=%s stuck on %s — routing to reasoned fallback",
                        chat_id, waiting_for)
            return _reasoned_fallback(
                bot_name, lang, user_text, reply, weave_hint=_FIELD_WEAVE_HINTS.get(waiting_for),
            )
        return reply

    def _continue_from_draft(
        self,
        chat_id: str,
        bot_name: str,
        draft: dict,
        lang: str,
        signup_actor: str | None = None,
    ) -> str:
        missing = _missing_prerequisite(draft)
        if missing:
            postgres.upsert_session(
                bot_name,
                chat_id,
                "trial_intake",
                {
                    "trial_id": draft["id"],
                    "waiting_for": missing,
                    "lang": lang,
                    "signup_actor": signup_actor,
                },
                draft["id"],
            )
            return _ask_missing(lang, missing, signup_actor)

        return self._evaluate_slots(chat_id, bot_name, draft, lang, signup_actor)

    def _evaluate_slots(
        self,
        chat_id: str,
        bot_name: str,
        draft: dict,
        lang: str,
        signup_actor: str | None = None,
    ) -> str:
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
                        "slots": _serialize_slots(fallback_slots),
                    },
                    draft["id"],
                )
                return _loc(
                    lang, "fallback_offer",
                    requested=_level_label(draft["experience"], lang),
                    offered=_level_label(offered, lang),
                )
            age_slots = trial_logic.get_birth_year_trial_slots(
                bot_name,
                int(draft["child_birth_year"]),
            )
            postgres.upsert_session(
                bot_name,
                chat_id,
                "trial_intake",
                {
                    "trial_id": draft["id"],
                    "waiting_for": None,
                    "lang": lang,
                    "signup_actor": signup_actor,
                },
                draft["id"],
            )
            return _no_groups_with_age_options(lang, age_slots, draft, signup_actor)

        return self._enter_day_or_group_selection(chat_id, bot_name, draft["id"], slots, lang)

    def _enter_day_or_group_selection(
        self, chat_id: str, bot_name: str, trial_id: int, slots: list[dict], lang: str,
    ) -> str:
        """Ask which day first, then which group — never a flat mixed list.

        A group meeting 3 times a week otherwise shows up as 3 near-duplicate
        rows (same group, same levels, different date), which is what made
        the old single flat list unreadable. Skips the day question when
        every eligible slot already falls on the same date.
        """
        dates = sorted({s["date"] for s in slots})
        if len(dates) == 1:
            return self._enter_group_selection_for_day(chat_id, bot_name, trial_id, slots, dates[0], lang)

        postgres.upsert_session(
            bot_name,
            chat_id,
            "trial_select_day",
            {
                "trial_id": trial_id,
                "slots": _serialize_slots(slots),
                "dates": [str(d) for d in dates],
                "lang": lang,
            },
            trial_id,
        )
        return f"{_loc(lang, 'choose_day')}\n\n{_day_lines(dates, lang)}"

    def _enter_group_selection_for_day(
        self, chat_id: str, bot_name: str, trial_id: int, slots: list[dict], chosen_date, lang: str,
    ) -> str:
        day_slots = [s for s in slots if str(s["date"]) == str(chosen_date)]
        postgres.upsert_session(
            bot_name,
            chat_id,
            "trial_select_slot",
            {
                "trial_id": trial_id,
                "slots": _serialize_slots(day_slots),
                "chosen_date": str(chosen_date),
                "lang": lang,
            },
            trial_id,
        )
        return (
            f"{_loc(lang, 'choose_slot', date=_fmt_date(chosen_date, lang))}"
            f"\n\n{_group_lines_for_day(day_slots, lang)}"
        )

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

        if state == "trial_select_day":
            dates = params.get("dates") or []
            all_slots = params.get("slots") or []
            interrupt = _session_interrupt_response(lang, user_text)
            if interrupt:
                return interrupt + "\n\n" + f"{_loc(lang, 'choose_day')}\n\n{_day_lines(dates, lang)}"
            if user_text.strip().isdigit():
                idx = int(user_text.strip()) - 1
                if idx < 0 or idx >= len(dates):
                    _log_rejected("day_choice", "out_of_range", user_text,
                                  trial_id=trial_id, options=len(dates))
                    return _loc(lang, "day_invalid")
                return self._enter_group_selection_for_day(
                    chat_id, bot_name, trial_id, all_slots, dates[idx], lang,
                )

            matched = _match_day_choice(user_text, history, dates)
            if matched:
                return self._enter_group_selection_for_day(
                    chat_id, bot_name, trial_id, all_slots, matched, lang,
                )

            _log_rejected("day_choice", "not_recognized", user_text, trial_id=trial_id)
            reminder = f"{_loc(lang, 'choose_day')}\n\n{_day_lines(dates, lang)}"
            return _reasoned_fallback(
                bot_name, lang, user_text, reminder,
                extra_hint=(
                    "Правило записи: пользователь должен указать дату, "
                    "прежде чем выбирать группу/тренера — если он называет "
                    "конкретную группу или тренера, не подтверждай выбор "
                    "группы сам, а мягко попроси сначала выбрать дату из списка."
                ),
            )

        if state == "trial_select_slot":
            slots = params.get("slots") or []
            chosen_date = params.get("chosen_date")
            header = _loc(lang, "choose_slot", date=_fmt_date(chosen_date, lang)) if chosen_date else _loc(lang, "choose_day")
            interrupt = _session_interrupt_response(lang, user_text)
            if interrupt:
                return interrupt + "\n\n" + f"{header}\n\n{_group_lines_for_day(slots, lang)}"
            if user_text.strip().isdigit():
                idx = int(user_text.strip()) - 1
                if idx < 0 or idx >= len(slots):
                    _log_rejected("slot_choice", "out_of_range", user_text,
                                  trial_id=trial_id, options=len(slots))
                    return _loc(lang, "slot_invalid")
                draft = academy_repo.get_trial(trial_id)
                return self._assign_slot_and_confirm(chat_id, bot_name, draft, slots[idx], lang)

            patch = _filled_patch(_extract_user_data(history, user_text))
            if not patch:
                _log_rejected("slot_choice", "not_a_number", user_text, trial_id=trial_id)
                reminder = f"{header}\n\n{_group_lines_for_day(slots, lang)}"
                return _reasoned_fallback(bot_name, lang, user_text, reminder)

            result = trial_service.update_intake(
                bot_name,
                trial_id,
                {
                    **patch,
                    "trial_day": None,
                    "start_time": None,
                    "end_time": None,
                    "group_id": None,
                    "language": lang,
                },
            )
            if not result["ok"]:
                return result.get("message") or _loc(lang, "no_groups")
            return self._continue_from_draft(
                chat_id, bot_name, result["data"]["trial"], lang, params.get("signup_actor")
            )

        if state == "trial_fallback_offer":
            decision = _confirm_decision(user_text) or ""
            slots = params.get("slots") or []
            if decision == "yes":
                return self._enter_day_or_group_selection(chat_id, bot_name, trial_id, slots, lang)
            if decision == "no":
                postgres.delete_session(bot_name, chat_id)
                return _loc(lang, "fallback_declined")
            reminder = _loc(
                lang, "fallback_offer",
                requested=_level_label(params.get("requested_level"), lang),
                offered=_level_label(params.get("offered_level"), lang),
            )
            return _reasoned_fallback(bot_name, lang, user_text, reminder)

        if state == "trial_confirm":
            decision = _confirm_decision(user_text) or ""
            if not decision:
                draft_now = academy_repo.get_trial(trial_id)
                patch = _real_changes(
                    _filled_patch(_extract_user_data(history, user_text)), draft_now,
                )
                if patch:
                    result = trial_service.update_intake(
                        bot_name,
                        trial_id,
                        {**patch, "language": lang},
                    )
                    if not result["ok"]:
                        return result.get("message") or _confirmation(academy_repo.get_trial(trial_id), lang)
                    return (
                        f"{_loc(lang, 'details_updated_confirm')}\n\n"
                        f"{_confirmation(result['data']['trial'], lang)}"
                    )
            if decision == "yes":
                draft = academy_repo.get_trial(trial_id)
                if not trial_logic.is_trial_slot_eligible(bot_name, draft):
                    result = trial_service.update_intake(
                        bot_name,
                        trial_id,
                        {
                            "trial_day": None,
                            "start_time": None,
                            "end_time": None,
                            "group_id": None,
                            "language": lang,
                        },
                    )
                    if not result["ok"]:
                        return result.get("message") or _loc(lang, "no_groups")
                    return (
                        f"{_loc(lang, 'slot_no_longer_eligible')}\n\n"
                        f"{self._continue_from_draft(chat_id, bot_name, result['data']['trial'], lang)}"
                    )
                result = trial_service.confirm_trial(bot_name, chat_id, trial_id)
                if not result["ok"]:
                    if result["code"] == "LIMIT_REACHED":
                        return _loc(lang, "reached_limits")
                    if result["code"] == "HAS_ACTIVE_TRIAL":
                        return _loc(lang, "has_active_trial")
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
            # decision is "" and nothing changed — an objection, a side
            # question, or anything else that isn't a value/yes/no. Answer it
            # instead of silently re-showing the identical confirmation card.
            reminder = _confirmation(academy_repo.get_trial(trial_id), lang)
            return _reasoned_fallback(bot_name, lang, user_text, reminder)

        return None
