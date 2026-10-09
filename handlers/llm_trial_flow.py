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

import contextvars
import functools
import inspect
import logging
import re
from datetime import date, datetime, timedelta, timezone

import config
from handlers.academy_extractor import extract_trial_details
from handlers.sessions.trial_session import _MY_TRIAL_KW
from integrations import trial_service
from integrations import trial as trial_logic
from integrations.repo import academy_repo
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
        "kk": "Дайындық деңгейін таңдаңыз:\n1. Бастапқы\n2. Орта\n3. Жоғары",
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
        "ru": "Подходящих групп сейчас не нашлось. Пожалуйста, позвоните администратору по номеру +7 700 555 6006.",
        "kk": "Қазір сәйкес топ табылмады. Әкімшіге +7 700 555 6006 нөміріне қоңырау шалыңыз.",
    },
    "preferred_unavailable": {
        "ru": "Указанное время не подходит для группы ребенка. Выберите один из доступных вариантов:",
        "kk": "Көрсетілген уақыт балаңызға сәйкес топқа келмейді. Қолжетімді нұсқалардың бірін таңдаңыз:",
    },
    "fallback_shown": {
        "ru": "Подходящей группы уровня {requested} сейчас нет. По возрасту и времени подходят группы уровнем ниже:",
        "kk": "Қазір {requested} деңгейіне сәйкес топ жоқ. Жасы мен уақыты бойынша бір деңгей төмен топтар сәйкес келеді:",
    },
    "choose_slot": {
        "ru": "Выберите группу на {date}:",
        "kk": "{date} күніне топты таңдаңыз:",
    },
    "choose_day": {
        "ru": "На какой день хотите записаться на пробное занятие?",
        "kk": "Сынақ сабағына қай күнге жазылғыңыз келеді?",
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
    "slot_no_longer_eligible": {
        "ru": "После изменения данных выбранная группа уже не подходит. Я убрал выбранные дату и время — выберите подходящий вариант заново.",
        "kk": "Деректер өзгергеннен кейін таңдалған топ сәйкес келмейді. Таңдалған күн мен уақытты алып тастадым — қолайлы нұсқаны қайта таңдаңыз.",
    },
}

# Warmer wording for the boxing bot. Keys missing here fall back to T, and the
# football bot keeps T as is. Numbered options, placeholders and the
# «моя запись» / «перенести запись» keywords must stay in sync with T — the
# parsers depend on them.
BOXING_T = {
    "ask_name": {
        "ru": "Подскажите, пожалуйста, как зовут ребёнка?",
        "kk": "Балаңыздың есімі кім, айтып жіберсеңіз?",
    },
    "ask_name_self": {
        "ru": "Подскажите, пожалуйста, как вас зовут?",
        "kk": "Есіміңіз кім, айтып жіберсеңіз?",
    },
    "birth_year_invalid": {
        "ru": "К сожалению, {year} год рождения нам не подходит. Мы принимаем детей от {min_age} до {max_age} лет. "
              "Подскажите, пожалуйста, год рождения ребёнка — например, *2016*.",
        "kk": "Өкінішке қарай, {year} туған жыл сәйкес келмейді. Біз {min_age} мен {max_age} жас аралығындағы балаларды қабылдаймыз. "
              "Балаңыздың туған жылын жазып жіберіңізші, мысалы: *2016*.",
    },
    "name_too_long": {
        "ru": "Имя получилось немного длинным. Пожалуйста, напишите его покороче — до {max_len} символов.",
        "kk": "Есім сәл ұзын болып кетті. {max_len} таңбаға дейін қысқартып жазыңызшы.",
    },
    "ask_birth_year": {
        "ru": "Подскажите, пожалуйста, год рождения ребёнка. Например: *2016*.",
        "kk": "Балаңыздың туған жылын жазып жіберіңізші. Мысалы: *2016*.",
    },
    "ask_experience": {
        "ru": "Какой у ребёнка уровень подготовки? Выберите, пожалуйста:\n1. Начинающий\n2. Средний\n3. Продвинутый\n\n"
              "Если ребёнок раньше не занимался боксом — смело выбирайте «Начинающий» 🥊",
        "kk": "Балаңыздың дайындық деңгейі қандай? Таңдаңызшы:\n1. Бастапқы\n2. Орта\n3. Жоғары\n\n"
              "Егер бала бұрын бокспен айналыспаса — «Бастапқы» деңгейін таңдаңыз 🥊",
    },
    "ask_school_shift": {
        "ru": "В какую смену ребёнок учится в школе? Так мы подберём удобное время.\n1. Утренняя\n2. Дневная",
        "kk": "Балаңыз мектепте қай ауысымда оқиды? Ыңғайлы уақытты таңдау үшін керек.\n1. Таңғы\n2. Түскі",
    },
    "no_groups": {
        "ru": "К сожалению, сейчас подходящей группы не нашлось. Но не переживайте — администратор с радостью поможет подобрать вариант.",
        "kk": "Өкінішке қарай, қазір сәйкес топ табылмады. Бірақ уайымдамаңыз — әкімші қолайлы нұсқаны таңдауға көмектеседі.",
    },
    "no_groups_call_admin": {
        "ru": "К сожалению, сейчас подходящей группы не нашлось. Пожалуйста, позвоните нашему администратору по номеру +7 700 555 2202 — там с радостью помогут!",
        "kk": "Өкінішке қарай, қазір сәйкес топ табылмады. Әкімшімізге +7 700 555 2202 нөміріне қоңырау шалыңызшы — міндетті түрде көмектеседі!",
    },
    "preferred_unavailable": {
        "ru": "К сожалению, в это время подходящей группы нет. Вот что можем предложить — выберите, пожалуйста, удобный вариант:",
        "kk": "Өкінішке қарай, бұл уақытта сәйкес топ жоқ. Мына нұсқалардың бірін таңдаңызшы:",
    },
    "fallback_shown": {
        "ru": "Группы уровня {requested} сейчас, к сожалению, нет. Зато по возрасту и времени подходят группы уровнем ниже:",
        "kk": "Өкінішке қарай, қазір {requested} деңгейіндегі топ жоқ. Бірақ жасы мен уақыты бойынша бір деңгей төмен топтар сәйкес келеді:",
    },
    "choose_slot": {
        "ru": "Вот группы на {date}. Выберите, пожалуйста, удобную:",
        "kk": "{date} күнгі топтар. Ыңғайлысын таңдаңызшы:",
    },
    "choose_day": {
        "ru": "На какой день вам удобно записаться на пробное занятие?",
        "kk": "Сынақ сабағына қай күн сізге ыңғайлы?",
    },
    "confirm": {
        "ru": "📋 Детали записи:\n📅 {date}\n⏰ {start}–{end}\n👤 Имя ребенка: {name}\n📆 Год рождения: {birth_year}\n🎯 Опыт: {experience}\n🏫 Смена: {school_shift}\n\nВсё верно? Подтверждаете запись?",
        "kk": "📋 Жазылым деректері:\n📅 {date}\n⏰ {start}–{end}\n👤 Балаңыздың есімі: {name}\n📆 Туған жылы: {birth_year}\n🎯 Тәжірибе: {experience}\n🏫 Ауысым: {school_shift}\n\nБәрі дұрыс па? Жазылымды растайсыз ба?",
    },
    "confirmed": {
        "ru": "Готово, вы записаны на пробное занятие! 🎉 Будем очень рады вас видеть!\n📅 {date}\n⏰ {start}–{end}\n👤 Имя ребенка: {name}\n\n"
              "Если появятся вопросы — пишите, мы всегда на связи.",
        "kk": "Дайын, сынақ сабағына жазылдыңыз! 🎉 Сізді асыға күтеміз!\n📅 {date}\n⏰ {start}–{end}\n👤 Балаңыздың есімі: {name}\n\n"
              "Сұрақтарыңыз болса — жазыңыз, біз әрқашан байланыстамыз.",
    },
    "declined": {
        "ru": "Хорошо, запись отменена. Если захотите записаться снова — просто напишите, будем рады!",
        "kk": "Жақсы, жазылым тоқтатылды. Қайта жазылғыңыз келсе — жазыңыз, қуана күтеміз!",
    },
    "reached_limits": {
        "ru": (
            "Похоже, по этому номеру пробное занятие уже было использовано. "
            "Если, по-вашему, это ошибка — администратор с радостью всё проверит."
        ),
        "kk": (
            "Бұл нөмір бойынша сынақ сабағы бұрын қолданылған сияқты. "
            "Егер бұл қате деп ойласаңыз — әкімші бәрін тексеріп береді."
        ),
    },
    "has_active_trial": {
        "ru": (
            "У вас уже есть запись на пробное занятие. "
            "Если хотите уточнить дату или перенести её, напишите: «моя запись» или «перенести запись»."
        ),
        "kk": (
            "Сізде сынақ сабағына жазылым бар. "
            "Күнін білгіңіз немесе ауыстырғыңыз келсе: «менің жазылымым» немесе «жазылымды ауыстыру» деп жазыңыз."
        ),
    },
    "slot_no_longer_eligible": {
        "ru": "После изменения данных выбранная группа, к сожалению, уже не подходит. Дату и время пришлось сбросить — выберите, пожалуйста, подходящий вариант заново.",
        "kk": "Деректер өзгергеннен кейін таңдалған топ, өкінішке қарай, сәйкес келмейді. Күн мен уақытты алып тастадым — қолайлы нұсқаны қайта таңдаңызшы.",
    },
}

_BOT_TEXTS = {"dopsy_boxing": BOXING_T}

# The bot whose flow is running. Set by the handler entry points (_bot_scoped)
# so _loc can pick per-bot wording without threading bot_name through every
# helper.
_current_bot: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trial_flow_bot", default=None,
)


def _bot_scoped(method):
    signature = inspect.signature(method)

    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        bot_name = signature.bind(*args, **kwargs).arguments.get("bot_name")
        token = _current_bot.set(bot_name)
        try:
            return method(*args, **kwargs)
        finally:
            _current_bot.reset(token)

    return wrapper

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
    "бастауыш": "Beginner",
    "бастаушы": "Beginner",
    "2": "Intermediate",
    "intermediate": "Intermediate",
    "средний": "Intermediate",
    "орта": "Intermediate",
    "3": "Advanced",
    "advanced": "Advanced",
    "продвинутый": "Advanced",
    "жоғары": "Advanced",
    "жетілген": "Advanced",
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
# A parent also says «мне/маған» ("маған баламды жаздыру керек"), so a child
# mentioned in the same message overrides the self-signup markers above.
# "сын" is matched by exact forms only — «сынақ» is Kazakh for "trial".
_CHILD_SUBJECT_RE = re.compile(
    r"\b(?:реб[её]н\w*|дет(?:и|ей|ям|ьми)|сын(?:а|у|ом|ок|ка|ишка|овей|овья)?|доч\w*|"
    r"внук\w*|внучк\w*|бала\w*|ұлым\w*|қызым\w*|немере\w*)\b",
    re.IGNORECASE,
)
# Lead-ins around a name in a reply to "what's the name?" — «менің атым Айдос»,
# «меня зовут Айдос», «баламның аты Айдос», «Айдос менің атым».
_NAME_LEAD_RE = re.compile(
    r"^(?:(?:менің|меним)\s+(?:атым|есімім|есимим)|атым|есімім|есимим|"
    r"(?:баламның|балаңыздың|ұлымның|қызымның)\s+(?:аты|есімі)|"
    r"(?:меня|его|её|ее|сына|дочь|дочку|реб[её]нка)\s+зовут|"
    r"(?:мо[её]|его|её|ее)\s+имя|я|мен)\s+",
    re.IGNORECASE,
)
_NAME_TAIL_RE = re.compile(
    r"\s+(?:(?:менің\s+)?(?:атым|есімім)|меня\s+зовут|(?:баламның\s+)?аты)$",
    re.IGNORECASE,
)
# «Мен Айдоспын» — the first-person copula suffix after «мен».
_KK_COPULA_RE = re.compile(r"^(\w{2,}?)(?:пын|бын|мын|пін|бін|мін)$", re.IGNORECASE)


def _loc(lang: str, key: str, **fmt) -> str:
    table = _BOT_TEXTS.get(_current_bot.get(), {})
    entry = table.get(key) or T[key]
    text = entry.get(lang) or entry["ru"]
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
    text = text or ""
    if _CHILD_SUBJECT_RE.search(text):
        return None
    return "self" if _SELF_SIGNUP_RE.search(text) else None


def _strip_name_lead(text: str) -> str:
    """«менің атым Айдос» / «меня зовут Айдос» / «Мен Айдоспын» -> «Айдос»."""
    value = (text or "").strip().strip(".,!;")
    stripped = _NAME_LEAD_RE.sub("", value, count=1)
    if stripped != value and value.lower().startswith("мен ") and " " not in stripped:
        match = _KK_COPULA_RE.match(stripped)
        if match:
            stripped = match.group(1)
    stripped = _NAME_TAIL_RE.sub("", stripped)
    return stripped.strip().strip(".,!;") or value


def _is_my_trial_query(text: str) -> bool:
    """True for "what trial(s) do I have" — reuses trial_session.py's proven
    keyword list rather than a second, drifting copy of the same intent.
    """
    lower = (text or "").lower()
    return any(kw in lower for kw in _MY_TRIAL_KW)


def _signup_actor_in(history: list, user_text: str) -> str | None:
    """_signup_actor over everything the client wrote — the flow keeps no
    session to remember an earlier «мне 15 лет»."""
    texts = [m.get("content") or "" for m in history or [] if m.get("role") == "user"]
    return _signup_actor("\n".join([*texts, user_text or ""]))


def _extract_user_data(
    history: list,
    user_text: str,
    waiting_for: str | None = None,
) -> tuple[dict, dict[str, tuple[str, object]]]:
    """LLM extraction, plus a local parse of the one field being asked for.

    The local parse is limited to `waiting_for`: run on every message it read
    a list choice like "2" as a level or a shift.

    Returns ``(extracted, rejected)`` — see _drop_invalid_fields.
    """
    extracted = extract_trial_details(history, user_text)
    if not extracted.get("child_birth_year"):
        extracted["child_birth_year"] = _birth_year_from_age(user_text)
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
    return extracted, rejected


_LATIN_TO_CYRILLIC = (
    ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("kh", "х"), ("ya", "я"), ("yu", "ю"),
    ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"), ("f", "ф"),
    ("g", "г"), ("h", "х"), ("i", "и"), ("j", "ж"), ("k", "к"), ("l", "л"),
    ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("q", "к"), ("r", "р"),
    ("s", "с"), ("t", "т"), ("u", "у"), ("v", "в"), ("w", "у"), ("x", "кс"),
    ("y", "ы"), ("z", "з"),
)


def _name_tokens(text: str | None) -> set[str]:
    """Lower-cased words of `text` in Cyrillic, so «Box Timur» meets «Тимур»."""
    value = (text or "").lower().replace("ё", "е")
    for latin, cyrillic in _LATIN_TO_CYRILLIC:
        value = value.replace(latin, cyrillic)
    return set(re.findall(r"\w{2,}", value))


def _is_trainer_name(bot_name: str, name: str) -> bool:
    """True when `name` is a word from a group name or trainer of this academy
    — the parent naming the class they want, not renaming the child."""
    wanted = _name_tokens(name)
    for info in academy_repo.get_groups_info(bot_name=bot_name):
        if wanted & _name_tokens(f"{info.get('group_name') or ''} {info.get('trainer') or ''}"):
            return True
    return False


def is_draft_in_progress(draft: dict) -> bool:
    """True while the client is still filling this draft in.

    Academy drafts are never swept, so an abandoned one must not keep the
    client in signup mode forever. Like the arena's draft TTL (and the session
    TTL this flow used to have), a draft untouched for BOOKING_SESSION_TTL is
    no longer in progress; its data is still reused if the client signs up
    again.
    """
    updated_at = draft.get("updated_at")
    if not isinstance(updated_at, datetime):
        return True
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - updated_at
    return age <= timedelta(seconds=config.BOOKING_SESSION_TTL)


# States of the session the LLM flow used to keep. It keeps none now; a row
# left from before that change is dropped where it is found.
LLM_FLOW_STATES = (
    "trial_intake", "trial_select_day", "trial_select_slot",
    "trial_fallback_offer", "trial_confirm",
)


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
        if "начина" in low or "нович" in low or "бастап" in low or "бастау" in low:
            return "Beginner"
        if "средн" in low or "орта" in low:
            return "Intermediate"
        if "продвин" in low or "жоғары" in low or "жетілген" in low:
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
        name = _strip_name_lead(text)
        if not _is_plausible_child_name(name):
            return None
        return name or None
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


# Draft columns that together hold the chosen class.
_CLEARED_SLOT = {"trial_day": None, "start_time": None, "end_time": None, "group_id": None}


def _iso_date(value) -> str | None:
    return str(value)[:10] if value not in (None, "") else None


def _hhmm(value) -> str | None:
    return str(value)[:5] if value not in (None, "") else None


def _chosen_slot_still_fits(draft: dict, slots: list[dict]) -> bool:
    """The draft's chosen class is still on offer and the parent hasn't since
    asked for a different date/time (e.g. picked another option from a list)."""
    day, start = _iso_date(draft.get("trial_day")), _hhmm(draft.get("start_time"))
    wanted_day, wanted_start = _iso_date(draft.get("preferred_date")), _hhmm(draft.get("preferred_time_start"))
    if wanted_day not in (None, day) or wanted_start not in (None, start):
        return False
    return any(
        int(s["group_id"]) == int(draft["group_id"])
        and str(s["date"]) == day
        and _hhmm(s["time_start"]) == start
        for s in slots
    )


def _narrow(
    slots: list[dict], draft: dict, lang: str, pick: bool = True, note_unavailable: bool = True,
) -> tuple[dict | None, str]:
    """Narrow `slots` by the parent's date/time (preferred_*).

    date + time → that class; date only → that day's classes; time only → the
    days with a class at that time; nothing → the list of days. Returns
    ``(slot, "")`` when exactly one class matches what the parent asked for
    (and `pick` is set), otherwise ``(None, options_text)``. A date/time that
    matches nothing is dropped with a "not available" note.
    """
    wanted_day = _iso_date(draft.get("preferred_date"))
    wanted_start = _hhmm(draft.get("preferred_time_start"))
    unavailable = False

    # A date already behind us is a leftover (an old draft or old history),
    # not something the parent is asking for now — drop it silently.
    if wanted_day and wanted_day < today_almaty().isoformat():
        wanted_day = None
    if wanted_day and wanted_day not in {str(s["date"]) for s in slots}:
        unavailable, wanted_day = True, None
    on_day = [s for s in slots if not wanted_day or str(s["date"]) == wanted_day]
    matches = [s for s in on_day if not wanted_start or _hhmm(s["time_start"]) == wanted_start]
    if not matches:
        unavailable, wanted_start, matches = True, None, on_day

    if pick and len(matches) == 1 and (wanted_day or wanted_start) and not unavailable:
        return matches[0], ""

    days = sorted({str(s["date"]) for s in matches})
    if len(days) == 1:
        body = (f"{_loc(lang, 'choose_slot', date=_fmt_date(days[0], lang))}"
                f"\n\n{_group_lines_for_day(matches, lang)}")
    else:
        body = f"{_loc(lang, 'choose_day')}\n\n{_day_lines(days, lang)}"
    if unavailable and note_unavailable:
        return None, f"{_loc(lang, 'preferred_unavailable')}\n\n{body}"
    return None, body


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
    """
    LLM-driven trial-signup handler — the academy counterpart of
    LlmBookingFlowHandler.

    Main entry point: handle(). The draft row (phone + state='draft') is the
    only flow state: every message is extracted, merged into the draft, and
    the draft is evaluated again (_evaluate_and_respond).
    """

    # ══════════════════════════════════════════════════════════════════════
    #  Main Entry Point
    # ══════════════════════════════════════════════════════════════════════

    @_bot_scoped
    def handle(
        self,
        chat_id: str,
        sender_phone: str,
        bot_name: str,
        user_text: str,
        history: list,
        lang: str,
    ) -> str:
        """Process one user message through the LLM trial flow."""
        # "какие у меня пробные" / "мои занятия" — answered before touching any
        # draft, so asking about an existing signup never creates an empty one.
        if _is_my_trial_query(user_text):
            from handlers.edit_trial import handle_trial_status_request
            return handle_trial_status_request(sender_phone, bot_name, lang)

        draft = academy_repo.get_existing_trial_draft(sender_phone, bot_name)

        # ── 1. A draft with a chosen class: yes/no confirms or cancels it ──
        if draft and draft.get("group_id"):
            decision = _confirm_decision(user_text)
            if decision == "yes":
                logger.info("[TRIAL] YES → confirm id=%d", draft["id"])
                return self._confirm(chat_id, bot_name, draft, lang)
            if decision == "no":
                logger.info("[TRIAL] NO → cancel id=%d", draft["id"])
                trial_service.cancel_trial(bot_name, chat_id, draft["id"], "user_declined_gated_trial")
                return _loc(lang, "declined")

        # On the opening message — or when coming back to a stale draft —
        # nothing has just been asked, so no field is "being answered": a
        # sentence like «балама сынақ сабағы керек» must not be parsed as the
        # child's name.
        asked_before = bool(draft) and is_draft_in_progress(draft)
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

        waiting_for = _missing_prerequisite(_draft_to_data(draft))
        signup_actor = _signup_actor_in(history, user_text)
        interrupt = _session_interrupt_response(lang, user_text, waiting_for)
        if interrupt:
            if waiting_for:
                return interrupt
            return f"{interrupt}\n\n{self._evaluate_and_respond(chat_id, bot_name, draft, lang, signup_actor)}"

        # ── 2. Extract and merge into the draft ──
        extracted, rejected = _extract_user_data(history, user_text, waiting_for if asked_before else None)
        if (extracted.get("child_name") and draft.get("child_name")
                and _is_trainer_name(bot_name, extracted["child_name"])):
            _log_rejected("child_name", "trainer_name", extracted["child_name"], trial_id=draft["id"])
            extracted["child_name"] = None
        logger.info("[TRIAL] Extracted for id=%d: %s", draft["id"], extracted)

        data = _merge(_draft_to_data(draft), extracted)
        result = trial_service.update_intake(bot_name, draft["id"], {**data, "language": lang})
        if not result["ok"]:
            return result.get("message") or _loc(lang, "no_groups")
        draft = result["data"]["trial"]

        # A value the client gave but the flow refused gets its own message,
        # so the client knows why the same field is asked again.
        missing = _missing_prerequisite(_draft_to_data(draft))
        reason, bad_value = rejected.get(missing, (None, None))
        if missing == "child_name" and reason == "too_long":
            logger.info("[TRIAL] Asking chat_id=%s for a shorter child_name (max %d chars)",
                        chat_id, CHILD_NAME_MAX_LEN)
            return _loc(lang, "name_too_long", max_len=CHILD_NAME_MAX_LEN)
        # Only a parseable-but-out-of-range year gets this message; a message
        # with no year in it at all is a non-answer, and the field is re-asked.
        if missing == "child_birth_year" and reason in ("in_future", "too_young", "too_old"):
            logger.info("[TRIAL] Asking chat_id=%s for a valid child_birth_year (age %d-%d)",
                        chat_id, CHILD_MIN_AGE, CHILD_MAX_AGE)
            return _loc(lang, "birth_year_invalid", year=bad_value,
                        min_age=CHILD_MIN_AGE, max_age=CHILD_MAX_AGE)

        # ── 3. Evaluate ──
        return self._evaluate_and_respond(chat_id, bot_name, draft, lang, signup_actor)

    def _confirm(self, chat_id: str, bot_name: str, draft: dict, lang: str) -> str:
        """Confirm the draft's chosen class, re-checking it still fits first."""
        if not trial_logic.is_trial_slot_eligible(bot_name, draft):
            result = trial_service.update_intake(
                bot_name, draft["id"],
                {**_CLEARED_SLOT, "preferred_date": None, "preferred_time_start": None, "language": lang},
            )
            if not result["ok"]:
                return result.get("message") or _loc(lang, "no_groups")
            return (
                f"{_loc(lang, 'slot_no_longer_eligible')}\n\n"
                f"{self._evaluate_and_respond(chat_id, bot_name, result['data']['trial'], lang)}"
            )

        result = trial_service.confirm_trial(bot_name, chat_id, draft["id"])
        if not result["ok"]:
            if result["code"] == "LIMIT_REACHED":
                return _loc(lang, "reached_limits")
            if result["code"] == "HAS_ACTIVE_TRIAL":
                return _loc(lang, "has_active_trial")
            return result.get("message") or _confirmation(academy_repo.get_trial(draft["id"]), lang)
        trial = result["data"]["trial"]
        return _loc(
            lang,
            "confirmed",
            date=_fmt_date(trial["trial_day"], lang),
            start=str(trial["start_time"])[:5],
            end=str(trial["end_time"])[:5],
            name=trial.get("child_name", ""),
        )

    def _assign_slot_and_confirm(
        self, chat_id: str, bot_name: str, draft: dict, slot: dict, lang: str
    ) -> str:
        result = trial_service.assign_slot(bot_name, draft["id"], slot)
        if not result["ok"]:
            return result.get("message") or _loc(lang, "no_groups")
        return _confirmation(result["data"]["trial"], lang)

    # ══════════════════════════════════════════════════════════════════════
    #  Core Evaluation
    # ══════════════════════════════════════════════════════════════════════

    @_bot_scoped
    def _evaluate_and_respond(
        self,
        chat_id: str,
        bot_name: str,
        draft: dict,
        lang: str,
        signup_actor: str | None = None,
    ) -> str:
        """Central dispatcher — the next reply follows from what the draft holds.

        Counterpart of LlmBookingFlowHandler._evaluate_and_respond: no step
        state, the draft alone decides. Intake comes first; after that the
        parent's date/time (preferred_*) narrows the classes the child is
        eligible for, and a single match is assigned straight away.
        """
        missing = _missing_prerequisite(_draft_to_data(draft))
        if missing:
            return _ask_missing(lang, missing, signup_actor)

        slots, note = self._bookable_slots(bot_name, draft, lang)
        if not slots:
            age_slots = trial_logic.get_birth_year_trial_slots(bot_name, int(draft["child_birth_year"]))
            return _no_groups_with_age_options(lang, age_slots, draft, signup_actor)

        if draft.get("group_id"):
            if _chosen_slot_still_fits(draft, slots):
                return _confirmation(draft, lang)
            result = trial_service.update_intake(bot_name, draft["id"], {**_CLEARED_SLOT, "language": lang})
            if not result["ok"]:
                return result.get("message") or _loc(lang, "no_groups")
            draft = result["data"]["trial"]

        reply = self._narrow_slots(chat_id, bot_name, draft, slots, lang)
        return f"{note}\n\n{reply}" if note else reply

    @staticmethod
    def _bookable_slots(bot_name: str, draft: dict, lang: str) -> tuple[list[dict], str | None]:
        """Classes the child can join, plus a note when only a lower level fits.

        The lower-level groups are listed right away rather than behind a
        yes/no question — picking one is the parent's agreement.
        """
        year, shift, level = int(draft["child_birth_year"]), draft["school_shift"], draft.get("experience")
        slots = trial_logic.get_eligible_trial_slots(bot_name, year, shift, experience=level)
        if slots:
            return slots, None
        lower = trial_logic.get_fallback_trial_slots(bot_name, year, shift, level)
        if not lower:
            return [], None
        return lower, _loc(lang, "fallback_shown", requested=_level_label(level, lang))

    def _narrow_slots(
        self, chat_id: str, bot_name: str, draft: dict, slots: list[dict], lang: str,
    ) -> str:
        """Assign the single class the parent's date/time points to, or list the options."""
        picked, text = _narrow(slots, draft, lang)
        if picked:
            return self._assign_slot_and_confirm(chat_id, bot_name, draft, picked, lang)
        return text

    @_bot_scoped
    def pending_prompt(self, bot_name: str, draft: dict | None, lang: str) -> str | None:
        """The question the flow is waiting on, worked out from the draft.

        Read-only — nothing is assigned or cleared — so it can be shown after
        a side answer and handed to the intent router.
        """
        if not draft:
            return None
        missing = _missing_prerequisite(_draft_to_data(draft))
        if missing:
            return _ask_missing(lang, missing)
        slots, note = self._bookable_slots(bot_name, draft, lang)
        if not slots:
            return None
        if draft.get("group_id") and _chosen_slot_still_fits(draft, slots):
            return _confirmation(draft, lang)
        # The "not available" note answered the message that asked for that
        # date; repeating it under every later side answer would nag.
        _, text = _narrow(slots, draft, lang, pick=False, note_unavailable=False)
        return f"{note}\n\n{text}" if note else text
