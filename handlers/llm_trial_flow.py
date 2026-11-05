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
from chat.system_prompts.academy_rules import ACADEMY_ADMIN_PHONES, academy_location, academy_what_to_bring
from handlers.academy_extractor import extract_trial_details
from integrations import trial_service
from integrations import trial as trial_logic
from integrations.repo import academy_repo
from utils import today_almaty

logger = logging.getLogger(__name__)


T = {
    # Questions come in three forms: the plain key while it is not known who
    # signs up, "_self" for the client themselves, "_other" for a child.
    "ask_name": {
        "ru": "Укажите имя того, кто будет заниматься.",
        "kk": "Сабаққа қатысатын адамның есімін жазыңыз.",
    },
    "ask_name_other": {
        "ru": "Укажите имя ребенка.",
        "kk": "Балаңыздың есімін жазыңыз.",
    },
    "ask_name_self": {
        "ru": "Укажите ваше имя.",
        "kk": "Атыңызды жазыңыз.",
    },
    "name_too_long": {
        "ru": "Имя слишком длинное — не больше {max_len} символов. Напишите, пожалуйста, короче.",
        "kk": "Есім тым ұзын — {max_len} таңбадан аспауы керек. Қысқарақ жазыңыз.",
    },
    "ask_birth_year": {
        "ru": "Укажите год рождения того, кто будет заниматься. Например: *2016*.",
        "kk": "Қатысушының туған жылын жазыңыз. Мысалы: *2016*.",
    },
    "ask_birth_year_other": {
        "ru": "Укажите год рождения ребенка. Например: *2016*.",
        "kk": "Балаңыздың туған жылын жазыңыз. Мысалы: *2016*.",
    },
    "ask_experience": {
        "ru": "Выберите уровень подготовки:\n1. Начинающий\n2. Средний\n3. Продвинутый",
        "kk": "Дайындық деңгейін таңдаңыз:\n1. Бастапқы\n2. Орта\n3. Жоғары",
    },
    "ask_experience_self": {
        "ru": "Выберите ваш уровень подготовки:\n1. Начинающий\n2. Средний\n3. Продвинутый",
        "kk": "Дайындық деңгейіңізді таңдаңыз:\n1. Бастапқы\n2. Орта\n3. Жоғары",
    },
    "ask_experience_other": {
        "ru": "Выберите уровень подготовки ребенка:\n1. Начинающий\n2. Средний\n3. Продвинутый",
        "kk": "Балаңыздың дайындық деңгейін таңдаңыз:\n1. Бастапқы\n2. Орта\n3. Жоғары",
    },
    "ask_school_shift": {
        "ru": "Какая школьная смена?\n1. Утренняя\n2. Дневная\n3. Ходит в детский сад",
        "kk": "Мектепте қай ауысымда оқиды?\n1. Таңғы\n2. Түскі\n3. Балабақшаға барады",
    },
    "ask_school_shift_self": {
        "ru": "В какую смену вы учитесь?\n1. Утренняя\n2. Дневная\n3. Хожу в детский сад",
        "kk": "Мектепте қай ауысымда оқисыз?\n1. Таңғы\n2. Түскі\n3. Балабақшаға барамын",
    },
    "ask_school_shift_other": {
        "ru": "Какая школьная смена у ребенка?\n1. Утренняя\n2. Дневная\n3. Ходит в детский сад",
        "kk": "Балаңыз қай ауысымда оқиды?\n1. Таңғы\n2. Түскі\n3. Балабақшаға барады",
    },
    "ask_birth_year_self": {
        "ru": "Укажите ваш возраст или год рождения. Например: *2010*.",
        "kk": "Жасыңызды немесе туған жылыңызды жазыңыз. Мысалы: *2010*.",
    },
    # Put in front of the first reply of a new signup. Empty: the football
    # bot's material doesn't say its trial is free.
    "trial_free_intro": {
        "ru": "",
        "kk": "",
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
        "ru": "Указанное время не подходит для этой группы. Выберите один из доступных вариантов:",
        "kk": "Көрсетілген уақыт бұл топқа келмейді. Қолжетімді нұсқалардың бірін таңдаңыз:",
    },
    "confirm": {
        "ru": "📋 Детали записи:\n📅 {date}\n⏰ {start}–{end}\n👤 Имя: {name}\n📆 Год рождения: {birth_year}\n🎯 Опыт: {experience}\n🏫 Смена: {school_shift}\n\nПодтвердить?",
        "kk": "📋 Жазылым деректері:\n📅 {date}\n⏰ {start}–{end}\n👤 Есімі: {name}\n📆 Туған жылы: {birth_year}\n🎯 Тәжірибе: {experience}\n🏫 Ауысым: {school_shift}\n\nРастайсыз ба?",
    },
    "confirmed": {
        "ru": "Запись на пробный урок оформлена, будем ждать!\n📅 {date}\n⏰ {start}–{end}\n👤 Имя: {name}",
        "kk": "Жазылым сәтті аяқталды, күтеміз!\n📅 {date}\n⏰ {start}–{end}\n👤 Есімі: {name}",
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
    # Asked before any signup question on a bot that has an adult group.
    # The numbering must stay in sync with _AUDIENCE_CHOICES.
    "choose_audience": {
        "ru": "К какой группе хотели бы записать?\n1. Детская группа ({child_min}–{child_max} лет)\n"
              "2. Взрослая группа ({adult_min}–{adult_max} лет)",
        "kk": "Қай топқа жазғыңыз келеді?\n1. Балалар тобы ({child_min}–{child_max} жас)\n"
              "2. Ересектер тобы ({adult_min}–{adult_max} жас)",
    },
    # The same question to a client signing up themselves («хочу записаться»).
    "choose_audience_self": {
        "ru": "К какой группе хотели бы записаться?\n1. Детская группа ({child_min}–{child_max} лет)\n"
              "2. Взрослая группа ({adult_min}–{adult_max} лет)",
        "kk": "Қай топқа жазылғыңыз келеді?\n1. Балалар тобы ({child_min}–{child_max} жас)\n"
              "2. Ересектер тобы ({adult_min}–{adult_max} жас)",
    },
    # An age outside the chosen group's range: the signup ends here.
    "age_no_group": {
        "ru": "К сожалению, для возраста {age} лет группы нет: {ranges}. Администратор подскажет вариант: {phone}.",
        "kk": "Өкінішке қарай, {age} жасқа арналған топ жоқ: {ranges}. Әкімші нұсқа ұсынады: {phone}.",
    },
    "choose_class": {
        "ru": "Выберите тренировку и напишите её номер:\n\n{lines}",
        "kk": "Жаттығуды таңдап, нөмірін жазыңыз:\n\n{lines}",
    },
    "choose_class_wished": {
        "ru": "Вот что подходит 🥊 Напишите номер тренировки:\n\n{lines}",
        "kk": "Сізге сәйкес жаттығулар 🥊 Нөмірін жазыңыз:\n\n{lines}",
    },
    "adult_unavailable": {
        "ru": "В это время тренировки взрослой группы нет.",
        "kk": "Бұл уақытта ересектер тобының жаттығуы жоқ.",
    },
    "confirm_adult": {
        "ru": "📋 Детали записи:\n👥 Взрослая группа\n📅 {date}\n⏰ {start}\n👤 Имя: {name}\n📆 Год рождения: {birth_year}\n\nВсё верно? Подтверждаете запись?",
        "kk": "📋 Жазылым деректері:\n👥 Ересектер тобы\n📅 {date}\n⏰ {start}\n👤 Есімі: {name}\n📆 Туған жылы: {birth_year}\n\nБәрі дұрыс па? Жазылымды растайсыз ба?",
    },
    "confirmed_adult": {
        "ru": "Готово, запись на пробное занятие оформлена! 🎉 Будем рады встрече!\n👥 Взрослая группа\n📅 {date}\n⏰ {start}\n👤 Имя: {name}",
        "kk": "Дайын, сынақ сабағына жазылым рәсімделді! 🎉 Кездесуді күтеміз!\n👥 Ересектер тобы\n📅 {date}\n⏰ {start}\n👤 Есімі: {name}",
    },
    # After a side answer at the summary, instead of the whole summary again.
    "confirm_or_questions": {
        "ru": "Подтверждаете запись или остались вопросы?",
        "kk": "Жазылымды растайсыз ба, әлде сұрақтарыңыз бар ма?",
    },
    # Instead of repeating a numbered list the last message already showed.
    "choose_from_above": {
        "ru": "Выберите, пожалуйста, номер из списка выше.",
        "kk": "Жоғарыдағы тізімнен нөмірін таңдаңызшы.",
    },
}

# Warmer wording for the boxing bot. Keys missing here fall back to T, and the
# football bot keeps T as is. Numbered options, placeholders and the
# «моя запись» / «перенести запись» keywords must stay in sync with T — the
# parsers depend on them.
BOXING_T = {
    "ask_name": {
        "ru": "Подскажите, пожалуйста, как зовут того, кто будет заниматься?",
        "kk": "Сабаққа кім қатысады, есімі кім, айтып жіберсеңіз?",
    },
    "ask_name_other": {
        "ru": "Подскажите, пожалуйста, как зовут ребёнка?",
        "kk": "Балаңыздың есімі кім, айтып жіберсеңіз?",
    },
    "ask_name_self": {
        "ru": "Подскажите, пожалуйста, как вас зовут?",
        "kk": "Есіміңіз кім, айтып жіберсеңіз?",
    },
    "name_too_long": {
        "ru": "Имя получилось немного длинным. Пожалуйста, напишите его покороче — до {max_len} символов.",
        "kk": "Есім сәл ұзын болып кетті. {max_len} таңбаға дейін қысқартып жазыңызшы.",
    },
    "ask_birth_year": {
        "ru": "Подскажите, пожалуйста, сколько лет тому, кто будет заниматься? Можно написать и год рождения, например: 2016.",
        "kk": "Қатысушы неше жаста? Туған жылын да жазуға болады, мысалы: 2016.",
    },
    "ask_birth_year_other": {
        "ru": "Подскажите, пожалуйста, сколько лет ребёнку? Можно написать и год рождения, например: 2016.",
        "kk": "Балаңыз неше жаста? Туған жылын да жазуға болады, мысалы: 2016.",
    },
    "ask_experience": {
        "ru": "Какой уровень подготовки? Выберите, пожалуйста:\n1. Начинающий\n2. Средний\n3. Продвинутый\n\n"
              "Если раньше не занимались боксом — смело выбирайте «Начинающий» 🥊",
        "kk": "Дайындық деңгейі қандай? Таңдаңызшы:\n1. Бастапқы\n2. Орта\n3. Жоғары\n\n"
              "Бұрын бокспен айналыспаған болса — «Бастапқы» деңгейін таңдаңыз 🥊",
    },
    "ask_experience_self": {
        "ru": "Какой у вас уровень подготовки? Выберите, пожалуйста:\n1. Начинающий\n2. Средний\n3. Продвинутый\n\n"
              "Если вы раньше не занимались боксом — смело выбирайте «Начинающий» 🥊",
        "kk": "Дайындық деңгейіңіз қандай? Таңдаңызшы:\n1. Бастапқы\n2. Орта\n3. Жоғары\n\n"
              "Бұрын бокспен айналыспаған болсаңыз — «Бастапқы» деңгейін таңдаңыз 🥊",
    },
    "ask_experience_other": {
        "ru": "Какой у ребёнка уровень подготовки? Выберите, пожалуйста:\n1. Начинающий\n2. Средний\n3. Продвинутый\n\n"
              "Если ребёнок раньше не занимался боксом — смело выбирайте «Начинающий» 🥊",
        "kk": "Балаңыздың дайындық деңгейі қандай? Таңдаңызшы:\n1. Бастапқы\n2. Орта\n3. Жоғары\n\n"
              "Егер бала бұрын бокспен айналыспаса — «Бастапқы» деңгейін таңдаңыз 🥊",
    },
    "ask_school_shift": {
        "ru": "В какую смену учёба в школе? Так мы подберём удобное время.\n1. Утренняя\n2. Дневная\n3. Ходит в детский сад",
        "kk": "Мектепте қай ауысымда оқиды? Ыңғайлы уақытты таңдау үшін керек.\n1. Таңғы\n2. Түскі\n3. Балабақшаға барады",
    },
    "ask_school_shift_self": {
        "ru": "В какую смену вы учитесь в школе? Так мы подберём удобное время.\n1. Утренняя\n2. Дневная\n3. Хожу в детский сад",
        "kk": "Мектепте қай ауысымда оқисыз? Ыңғайлы уақытты таңдау үшін керек.\n1. Таңғы\n2. Түскі\n3. Балабақшаға барамын",
    },
    "ask_school_shift_other": {
        "ru": "В какую смену ребёнок учится в школе? Так мы подберём удобное время.\n1. Утренняя\n2. Дневная\n3. Ходит в детский сад",
        "kk": "Балаңыз мектепте қай ауысымда оқиды? Ыңғайлы уақытты таңдау үшін керек.\n1. Таңғы\n2. Түскі\n3. Балабақшаға барады",
    },
    "ask_birth_year_self": {
        "ru": "Подскажите, пожалуйста, сколько вам лет?",
        "kk": "Жасыңыз нешеде? Туған жылыңызды да жазуға болады, мысалы: 2000.",
    },
    "trial_free_intro": {
        "ru": "Пробное занятие у нас бесплатное 🥊",
        "kk": "Сынақ сабағы тегін 🥊",
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
    "confirm": {
        "ru": "📋 Детали записи:\n📅 {date}\n⏰ {start}–{end}\n👤 Имя: {name}\n📆 Год рождения: {birth_year}\n🎯 Опыт: {experience}\n🏫 Смена: {school_shift}\n\nВсё верно? Подтверждаете запись?",
        "kk": "📋 Жазылым деректері:\n📅 {date}\n⏰ {start}–{end}\n👤 Есімі: {name}\n📆 Туған жылы: {birth_year}\n🎯 Тәжірибе: {experience}\n🏫 Ауысым: {school_shift}\n\nБәрі дұрыс па? Жазылымды растайсыз ба?",
    },
    "confirmed": {
        "ru": "Готово, запись на пробное занятие оформлена! 🎉 Будем очень рады встрече!\n📅 {date}\n⏰ {start}–{end}\n👤 Имя: {name}\n\n"
              "Если появятся вопросы — пишите, мы всегда на связи.",
        "kk": "Дайын, сынақ сабағына жазылым рәсімделді! 🎉 Кездесуді асыға күтеміз!\n📅 {date}\n⏰ {start}–{end}\n👤 Есімі: {name}\n\n"
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
            "По этому номеру уже есть запись на пробное занятие. "
            "Если хотите уточнить дату или перенести её, напишите: «моя запись» или «перенести запись»."
        ),
        "kk": (
            "Бұл нөмір бойынша сынақ сабағына жазылым бар. "
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


# "self" while the client signs up themselves, "other" for a child, None while
# unknown — _loc then picks a key's "_self" / "_other" variant where one exists.
_current_actor: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trial_flow_actor", default=None,
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
        "kindergarten": "детский сад",
    },
    "kk": {
        "morning": "таңғы",
        "afternoon": "түскі",
        "kindergarten": "балабақша",
    },
}



# ── Audience: children's group or the adult group ──
# Ages of the children's and the adult group. An age outside the chosen
# group's range ends the signup with the admin's number.
CHILD_AGES = (3, 16)
ADULT_AGES = (17, 60)
ADULT_MIN_AGE = ADULT_AGES[0]
# Academies that never ask the group: a client who states none is a child.
_CHILD_ONLY_BOTS = ("dopsy_fs_school",)


def _loc(lang: str, key: str, **fmt) -> str:
    table = _BOT_TEXTS.get(_current_bot.get(), {})
    actor = _current_actor.get()
    if actor in ("self", "other") and (f"{key}_{actor}" in table or f"{key}_{actor}" in T):
        key = f"{key}_{actor}"
    entry = table.get(key) or T[key]
    text = entry.get(lang) or entry["ru"]
    return text.format(**fmt) if fmt else text


def _stored_audience(draft: dict) -> str | None:
    """The draft's group. A draft from before the group choice existed that
    already holds a child's birth year is a children's signup."""
    if draft.get("audience") in ("child", "adult"):
        return draft["audience"]
    if draft.get("child_birth_year") not in (None, ""):
        return "child"
    return None


def _audience(bot_name: str, draft: dict) -> str | None:
    audience = _stored_audience(draft)
    if audience is None and bot_name in _CHILD_ONLY_BOTS:
        return "child"
    return audience


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


def _draft_idle(draft: dict) -> timedelta | None:
    updated_at = draft.get("updated_at")
    if not isinstance(updated_at, datetime):
        return None
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - updated_at


def is_draft_abandoned(draft: dict) -> bool:
    """True once nobody touched the draft for TRIAL_DRAFT_KEEP_SECONDS — it is
    then cancelled (abandon_draft)."""
    idle = _draft_idle(draft)
    return idle is not None and idle > timedelta(seconds=config.TRIAL_DRAFT_KEEP_SECONDS)


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


# academy_trials.child_name is VARCHAR(30); a longer value fails the UPDATE.
CHILD_NAME_MAX_LEN = trial_service.TRIAL_TEXT_LIMITS["child_name"]




def _audience_prompt(lang: str, signup_for: str | None = None) -> str:
    key = "choose_audience" if signup_for == "other" else "choose_audience_self"
    return _loc(lang, key, child_min=CHILD_AGES[0], child_max=CHILD_AGES[1],
                adult_min=ADULT_AGES[0], adult_max=ADULT_AGES[1])


def _log_rejected(field: str, reason: str, value, **context) -> None:
    """One log format for every intake value the flow refuses to store."""
    extra = "".join(f" {k}={v}" for k, v in context.items())
    logger.info("[TRIAL] %s rejected: reason=%s%s value=%r", field, reason, extra, value)


def _child_name_rejection(text: str | None) -> str | None:
    """Why an extracted name cannot be stored, or None if it can. What counts
    as a name is the extractor's call; this only guards the column."""
    if not text:
        return "empty"
    value = text.strip()
    if not value:
        return "empty"
    if "?" in value:
        return "question"
    if not any(ch.isalpha() for ch in value):
        return "no_letters"
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


def _waiting_prompt(lang: str, waiting_for: str | None, signup_actor: str | None = None) -> str:
    if not waiting_for:
        return ""
    prompt = _ask_missing(lang, waiting_for, signup_actor)
    if lang == "ru":
        return f"\n\nЧтобы продолжить запись на пробное занятие: {prompt}"
    return f"\n\nСынақ сабағына жазылуды жалғастыру үшін: {prompt}"


def _session_interrupt_response(
    lang: str, intent: str | None, waiting_for: str | None = None, signup_actor: str | None = None,
) -> str | None:
    if intent == "bot_identity":
        base = (
            "Я бот-ассистент академии. Могу ответить на вопросы о тренировках "
            "и помочь записаться на пробное занятие."
            if lang == "ru"
            else "Мен академияның бот-ассистентімін. Жаттығулар туралы сұрақтарға "
                 "жауап беріп, сынақ сабағына жазуға көмектесемін."
        )
        return base + _waiting_prompt(lang, waiting_for, signup_actor)

    if intent == "greeting":
        base = "Здравствуйте!" if lang == "ru" else "Сәлеметсіз бе!"
        return base + _waiting_prompt(lang, waiting_for, signup_actor)

    if intent == "thanks":
        base = "Хорошо." if lang == "ru" else "Жақсы."
        return base + _waiting_prompt(lang, waiting_for, signup_actor)

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


def _ask_missing(lang: str, field: str, signup_actor: str | None = None, history: list | None = None) -> str:
    """The question for `field`. With `history` the LLM phrases the question
    for this chat; the numbered options stay as written (the parsers read
    them), and the fixed text is the fallback."""
    if field == "audience":
        return _audience_prompt(lang)
    # The "_self" / "_other" / neutral form of the question for this signup.
    token = _current_actor.set(signup_actor)
    try:
        text = _loc(lang, {
            "child_name": "ask_name",
            "child_birth_year": "ask_birth_year",
            "experience": "ask_experience",
            "school_shift": "ask_school_shift",
        }[field])
        return _phrased(lang, text, history)
    finally:
        _current_actor.reset(token)


def _phrased(lang: str, text: str, history: list | None) -> str:
    """`text`'s question line phrased by the LLM for this chat, with its
    numbered options kept as written; `text` itself without history or on
    failure."""
    if not history:
        return text
    question, *rest = text.split("\n")
    options = [line for line in rest if re.match(r"\d+\.", line)]
    try:
        from chat import llm
        phrased = llm.get_question_reply(lang, question, history, _current_actor.get())
    except Exception:
        logger.exception("trial question phrasing failed")
        return text
    return "\n".join([phrased, *options]) if phrased else text


def _level_label(value: str | None, lang: str) -> str:
    return LEVEL_LABELS.get(lang, LEVEL_LABELS["ru"]).get(value, value or "")


def _shift_label(value: str | None, lang: str) -> str:
    return SHIFT_LABELS.get(lang, SHIFT_LABELS["ru"]).get(value, value or "")


def _trainer_suffix(slot: dict) -> str:
    trainer = (slot.get("trainer") or "").strip()
    return f" — {trainer}" if trainer else ""


def _slot_trainer(draft: dict, slots: list[dict]) -> str | None:
    """Trainer of the draft's chosen class, read from the slots at hand."""
    group_id = draft.get("group_id")
    return next((s.get("trainer") for s in slots
                 if group_id is not None and int(s["group_id"]) == int(group_id)), None)


def _with_trainer(body: str, trainer: str | None, lang: str) -> str:
    """Add the trainer under the ⏰ line of a confirmation."""
    if not trainer:
        return body
    lines = body.split("\n")
    at = next((i for i, line in enumerate(lines) if line.startswith("⏰")), None)
    if at is None:
        return body
    label = "Тренер" if lang == "ru" else "Жаттықтырушы"
    lines.insert(at + 1, f"👨‍🏫 {label}: {trainer}")
    return "\n".join(lines)


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
            f"{i}. {group}{level_text}{_trainer_suffix(slot)}\n"
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


def _confirmation(draft: dict, lang: str, trainer: str | None = None) -> str:
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
    return _with_trainer(body, trainer, lang) + _change_hint(lang)


def _change_hint(lang: str) -> str:
    """Under a summary: «да» confirms, «нет» cancels, a change is applied."""
    return "\n\nЕсли нужно что-то изменить, подскажите." if lang == "ru" else \
        "\n\nБір нәрсені өзгерту керек болса, айтыңыз."


def _adult_confirmation(draft: dict, lang: str, trainer: str | None = None) -> str:
    body = _loc(
        lang,
        "confirm_adult",
        date=_fmt_date(draft["trial_day"], lang),
        start=str(draft["start_time"])[:5],
        name=draft.get("child_name", ""),
        birth_year=draft.get("child_birth_year", ""),
    )
    return _with_trainer(body, trainer, lang) + _change_hint(lang)


def _numbered_list(text: str | None) -> bool:
    return bool(text and re.search(r"^\s*1\. ", text, re.MULTILINE))


def _last_bot_message(history: list | None) -> str:
    return next((m.get("content") or "" for m in reversed(history or [])
                 if m.get("role") == "assistant"), "")


def shorten_repeated_list(text: str | None, history: list | None, lang: str) -> str | None:
    """A numbered list the client was shown in the last bot message becomes a
    short "choose from the list above" — the same list again would only bury
    the answer to what they just asked."""
    if _numbered_list(text) and text.strip() in _last_bot_message(history):
        return _loc(lang, "choose_from_above")
    return text


# ── Signup steps ──
# Child: name → age → experience → school shift → class → confirm.
# Adult: name → age → class → confirm. A step is "missing" while its draft
# column is empty; the first missing one is asked.
_CHILD_STEPS = ("child_name", "child_birth_year", "experience", "school_shift")
_ADULT_STEPS = ("child_name", "child_birth_year")
_LEVELS = ("Beginner", "Intermediate", "Advanced")
_SHIFTS = ("morning", "afternoon", "kindergarten")
# A pick from a numbered list: «2», «2.», «№2», «2-ші».
_PICK_RE = re.compile(r"^\s*(?:№\s*)?(\d{1,2})\s*(?:[.)]|-?(?:ші|шы|ой|ий|й|я))?\s*$", re.IGNORECASE)
_LIST_LINE_RE = re.compile(r"^\s*(\d{1,2})\.\s+(.+?)\s*$", re.MULTILINE)
_DAY_MARK = "📅"
_CLASS_LIST_MAX = 8


def _age_of(year) -> int:
    return today_almaty().year - int(year)


def _birth_year(value) -> int | None:
    """The extractor's birth year, or None when it isn't one."""
    try:
        year = int(value or 0)
    except (TypeError, ValueError):
        return None
    return year if 1900 <= year <= today_almaty().year else None


def _class_line(slot: dict, adult: bool) -> str:
    """«17:00–18:00 — Тренер», under its day. The adult group's end time is
    technical (time_end is NOT NULL) and is never shown."""
    time_text = _hhmm(slot["time_start"]) if adult else f"{_hhmm(slot['time_start'])}–{_hhmm(slot['time_end'])}"
    return f"{time_text}{_trainer_suffix(slot)}"


def _class_list(slots: list[dict], lang: str, adult: bool, narrowed: bool = False) -> str:
    """Classes under a heading per day, numbered straight through."""
    lines, day = [], None
    for number, slot in enumerate(slots[:_CLASS_LIST_MAX], 1):
        if _fmt_date(slot["date"], lang) != day:
            day = _fmt_date(slot["date"], lang)
            lines += ["", f"{_DAY_MARK} {day}"]
        lines.append(f"{number}. {_class_line(slot, adult)}")
    return _loc(lang, "choose_class_wished" if narrowed else "choose_class", lines="\n".join(lines[1:]))


def _shown_classes(message: str) -> dict[int, tuple[str | None, str]]:
    """A class list as the client saw it: number → (day heading, class line)."""
    shown, day = {}, None
    for line in message.splitlines():
        if line.startswith(_DAY_MARK):
            day = line[len(_DAY_MARK):].strip()
        elif match := _LIST_LINE_RE.match(line):
            shown[int(match.group(1))] = (day, match.group(2))
    return shown


def _picked_from_list(text: str | None, history: list | None, slots: list[dict], adult: bool) -> dict | None:
    """The class a «2» picks: line 2 of the latest class list the client was
    shown, so a list narrowed to one trainer keeps its own numbering."""
    match = _PICK_RE.match(text or "")
    if not match:
        return None
    number = int(match.group(1))
    by_line = {(_fmt_date(s["date"], lang), _class_line(s, adult)): s for s in slots for lang in ("ru", "kk")}
    for message in reversed(history or []):
        if message.get("role") != "assistant":
            continue
        lines = _shown_classes(message.get("content") or "")
        if any(line in by_line for line in lines.values()):
            return by_line.get(lines.get(number))
    return slots[number - 1] if 1 <= number <= len(slots) else None


def _real_date(value) -> str | None:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _real_time(value) -> str | None:
    try:
        return datetime.strptime(str(value)[:5], "%H:%M").strftime("%H:%M")
    except ValueError:
        return None


def _wish(source: dict) -> dict:
    """What the client wishes for in a class, read from a draft or the
    extractor, keyed by draft column. Only real dates and times are kept."""
    return {
        "preferred_date": _real_date(source.get("preferred_date")),
        "preferred_time_start": _real_time(source.get("preferred_time_start")),
        "preferred_time_end": _real_time(source.get("preferred_time_end")),
        "preferred_weekdays": sorted({d for d in source.get("preferred_weekdays") or [] if d in range(7)}) or None,
        "preferred_trainers": sorted({t.strip() for t in source.get("preferred_trainers") or [] if t.strip()}) or None,
    }


def _wished_classes(slots: list[dict], wish: dict) -> list[dict]:
    """The classes that fit every wish. A start alone is that exact time; with
    an end it is a range the class has to start in."""
    fits = slots
    if wish["preferred_trainers"]:
        wanted = _name_tokens(" ".join(wish["preferred_trainers"]))
        fits = [s for s in fits if wanted & _name_tokens(s.get("trainer"))]
    start, end = wish["preferred_time_start"], wish["preferred_time_end"]
    if end:
        fits = [s for s in fits if (start or "00:00") <= _hhmm(s["time_start"]) < end]
    elif start:
        fits = [s for s in fits if _hhmm(s["time_start"]) == start]
    day = wish["preferred_date"]
    if wish["preferred_weekdays"]:
        fits = [s for s in fits if date.fromisoformat(_iso_date(s["date"])).weekday() in wish["preferred_weekdays"]]
    elif day and day >= today_almaty().isoformat():
        that_day = [s for s in fits if _iso_date(s["date"]) == day]
        weekday = date.fromisoformat(day).weekday()
        fits = that_day or [s for s in fits if date.fromisoformat(_iso_date(s["date"])).weekday() == weekday]
    return fits


def _classes_on_offer(slots: list[dict], wish: dict) -> tuple[list[dict], bool]:
    """The classes to show, and whether the wish could not be met. Nothing
    fits: the closest ones, giving up the time first, the trainer last."""
    shown = _wished_classes(slots, wish)
    if shown:
        return shown, False
    closest = dict(wish)
    for fields in (("preferred_time_start", "preferred_time_end"),
                   ("preferred_date", "preferred_weekdays"), ("preferred_trainers",)):
        closest.update(dict.fromkeys(fields))
        shown = _wished_classes(slots, closest)
        if shown:
            break
    return shown, True


def _slot_on_offer(draft: dict, slots: list[dict]) -> bool:
    """The draft's chosen class is still among `slots`."""
    return any(
        int(s["group_id"]) == int(draft["group_id"])
        and _iso_date(s["date"]) == _iso_date(draft.get("trial_day"))
        and _hhmm(s["time_start"]) == _hhmm(draft.get("start_time"))
        for s in slots
    )


class LlmTrialFlowHandler:
    """
    LLM-driven trial-signup handler — the academy counterpart of
    LlmBookingFlowHandler.

    Main entry point: handle(). The draft row (phone + state='draft') is the
    only flow state: every message's answers are saved to the draft, then the
    first missing step is asked (_evaluate_and_respond). A side question goes
    through absorb(), so its answers are saved too.
    """

    # The last message's answer was lost to an extractor failure: the caller
    # asks again, then hands over to the admin.
    extractor_failed = False

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
        intent: str | None = None,
    ) -> str:
        """Process one user message through the LLM trial flow. `intent` is the
        router's reading of it; None means the router could not tell."""
        self._new_draft = self._asks_about_trial = self._wants_change = self.extractor_failed = False
        # The wording is about the child until the adult group is chosen.
        token = _current_actor.set(None)
        try:
            full = self._handle(chat_id, sender_phone, bot_name, user_text, history, lang, intent)
            reply = shorten_repeated_list(full, history, lang)
            if reply != full:
                # A bare "choose from the list" would ignore what the client
                # just said — the LLM answers it in a word, then asks again.
                reply = _phrased(lang, reply, [*(history or []), {"role": "user", "content": user_text}])
        finally:
            _current_actor.reset(token)
        # A new signup opens with "the trial is free", then the first question.
        # Only when the message is about the trial lesson or its price.
        intro = _loc(lang, "trial_free_intro") if self._new_draft and self._asks_about_trial else ""
        return f"{intro}\n\n{reply}" if intro else reply

    @_bot_scoped
    def absorb(
        self,
        chat_id: str,
        sender_phone: str,
        bot_name: str,
        user_text: str,
        history: list,
        lang: str,
    ) -> None:
        """Save the signup answers a side question carries («8 жаста, бағасы
        қанша?») so they are not asked again. Replies nothing — the question
        is answered elsewhere."""
        draft = academy_repo.get_existing_trial_draft(sender_phone, bot_name)
        if draft:
            self._save_answers(bot_name, draft, user_text, history, lang, answering=None)

    def _handle(
        self,
        chat_id: str,
        sender_phone: str,
        bot_name: str,
        user_text: str,
        history: list,
        lang: str,
        intent: str | None,
    ) -> str:
        draft = academy_repo.get_existing_trial_draft(sender_phone, bot_name)
        if draft and is_draft_abandoned(draft):
            trial_service.abandon_draft(bot_name, chat_id, draft)
            draft, history = None, []

        # ── 1. The summary: the router's yes confirms, its no cancels ──
        at_summary = bool(draft) and self._next_step(bot_name, draft) == "confirm"
        if at_summary:
            if intent == "confirm_yes":
                logger.info("[TRIAL] YES → confirm id=%d", draft["id"])
                return self._confirm(chat_id, bot_name, draft, lang)
            if intent == "confirm_no":
                return self._decline(chat_id, bot_name, draft, lang)

        # The step this message answers. The opening message answers none: a
        # sentence like «балама сынақ сабағы керек» must not become a name.
        answering = self._next_step(bot_name, draft) if draft else None
        if not draft:
            result = trial_service.create_or_get_draft(bot_name, chat_id, sender_phone, lang)
            if not result["ok"]:
                if result["code"] == "HAS_ACTIVE_TRIAL":
                    return _loc(lang, "has_active_trial")
                return result.get("message") or _loc(lang, "no_groups")
            draft = result["data"]["trial"]
            self._new_draft = True

        interrupt = _session_interrupt_response(lang, intent)
        if interrupt:
            return f"{interrupt}\n\n{self._evaluate_and_respond(chat_id, bot_name, draft, lang)}"

        # ── 2. Save the answers ──
        draft, problem, wanted, error = self._save_answers(
            bot_name, draft, user_text, history, lang, answering,
        )
        if error:
            return error
        if problem == "name_too_long":
            return _loc(lang, "name_too_long", max_len=CHILD_NAME_MAX_LEN)
        if isinstance(problem, int):
            logger.info("[TRIAL] Age %d out of range — signup id=%d ends", problem, draft["id"])
            trial_service.cancel_trial(bot_name, chat_id, draft["id"], "age_out_of_range")
            return self._age_no_group(bot_name, problem, lang)
        if at_summary:
            # Another day, time or trainer, or «поменяйте время»: the class
            # is picked again. Otherwise the summary is shown again.
            if (wanted or self._wants_change) and draft.get("group_id"):
                result = trial_service.update_intake(bot_name, draft["id"], {**_CLEARED_SLOT, "language": lang})
                if not result["ok"]:
                    return result.get("message") or _loc(lang, "no_groups")
                draft = result["data"]["trial"]

        # ── 3. Next step ──
        return self._evaluate_and_respond(
            chat_id, bot_name, draft, lang,
            user_text=user_text, history=history, wanted=wanted, picking=answering == "slot",
        )

    # ══════════════════════════════════════════════════════════════════════
    #  Answers
    # ══════════════════════════════════════════════════════════════════════

    def _save_answers(
        self, bot_name: str, draft: dict, user_text: str, history: list, lang: str, answering: str | None,
    ) -> tuple[dict, str | int | None, bool, str | None]:
        """Read the message's answers into the draft.

        Returns ``(draft, problem, wanted, error)``: problem is the age that
        fits no group (int) or "name_too_long", wanted whether the message
        changed what the client wishes for, error a reply when the save failed.
        """
        fields, problem, wanted = self._answers(bot_name, draft, user_text, history, answering)
        if not fields:
            return draft, problem, wanted, None
        # A new age, level or shift can make the chosen class unfit.
        if draft.get("group_id") and set(fields) & {"child_birth_year", "experience", "school_shift"}:
            fields.update(_CLEARED_SLOT)
        logger.info("[TRIAL] Saving for id=%d: %s", draft["id"], fields)
        result = trial_service.update_intake(bot_name, draft["id"], {**fields, "language": lang})
        if not result["ok"]:
            return draft, None, wanted, result.get("message") or _loc(lang, "no_groups")
        return result["data"]["trial"], problem, wanted, None

    def _answers(
        self, bot_name: str, draft: dict, user_text: str, history: list, answering: str | None,
    ) -> tuple[dict, str | int | None, bool]:
        """The draft fields the message gives, plus (problem, wanted).

        The extractor reads the client's words; here its values are only
        checked against the group and the column.
        """
        stored = _wish(draft)
        extracted = extract_trial_details(history, user_text, answering, stored)
        self._asks_about_trial = bool(extracted.get("asks_about_trial_or_price"))
        self._wants_change = bool(extracted.get("wants_change"))
        # A pick from the class list needs no extractor.
        self.extractor_failed = bool(extracted.get("failed")) and answering != "slot"
        fields, problem = {}, None

        # The extractor reads the whole chat, so its wish replaces the stored
        # one; it goes in with the other answers.
        if not extracted.get("failed"):
            fields = {key: value for key, value in _wish(extracted).items() if value != stored[key]}
        wanted = bool(fields)

        # The group the client states is kept, even with no adult class on
        # offer: the class step then sends them to the admin.
        audience = _stored_audience(draft)
        if audience is None and extracted.get("audience") in ("child", "adult"):
            audience = extracted["audience"]
        audience = audience or _audience(bot_name, draft)
        if audience and audience != draft.get("audience"):
            fields["audience"] = audience
        # Who signs up — the latest statement wins.
        if extracted.get("signup_for") in ("self", "other") and extracted["signup_for"] != draft.get("signup_for"):
            fields["signup_for"] = extracted["signup_for"]
        if audience is None:
            return fields, None, wanted

        # A name already on the draft changes only to one the message states.
        candidate = extracted.get("child_name")
        if answering == "child_name" and _child_name_rejection(candidate) == "too_long":
            problem = "name_too_long"
        if (candidate and not re.search(r"\d", candidate)
                and _is_plausible_child_name(candidate)
                and (answering == "child_name" or not _is_trainer_name(bot_name, candidate))
                and (not draft.get("child_name") or candidate.lower() in (user_text or "").lower())
                and candidate != draft.get("child_name")):
            fields["child_name"] = candidate

        # Age, kept as a birth year. Outside the group's range it ends or moves
        # the signup only as the answer to the age question or a summary fix.
        year = _birth_year(extracted.get("child_birth_year"))
        if year:
            low, high = ADULT_AGES if audience == "adult" else CHILD_AGES
            age = _age_of(year)
            if not low <= age <= high:
                if answering not in ("child_birth_year", "confirm"):
                    year = None
                elif audience == "child" and ADULT_AGES[0] <= age <= ADULT_AGES[1]:
                    # Too old for the children — the same signup moves on to the
                    # adult group, keeping what was already given.
                    fields["audience"] = audience = "adult"
                else:
                    problem, year = age, None
        if year and year != draft.get("child_birth_year"):
            fields["child_birth_year"] = year

        # Level and school shift (children only)
        if audience == "child":
            for field, allowed in (("experience", _LEVELS), ("school_shift", _SHIFTS)):
                value = extracted.get(field)
                if value in allowed and value != draft.get(field):
                    fields[field] = value
        return fields, problem, wanted

    # ══════════════════════════════════════════════════════════════════════
    #  Steps
    # ══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _decline(chat_id: str, bot_name: str, draft: dict, lang: str) -> str:
        logger.info("[TRIAL] Summary declined → cancel id=%d", draft["id"])
        trial_service.cancel_trial(bot_name, chat_id, draft["id"], "user_declined_gated_trial")
        return _loc(lang, "declined")

    @staticmethod
    def _signup_actor(draft: dict) -> str | None:
        """"self" when the client signs up themselves, "other" for someone
        else — the draft's signup_for, saved from the extractor."""
        return draft.get("signup_for") if draft.get("signup_for") in ("self", "other") else None

    def _next_step(self, bot_name: str, draft: dict) -> str:
        """"audience", a missing field, "slot" or "confirm"."""
        audience = _audience(bot_name, draft)
        if audience is None:
            return "audience"
        data = _draft_to_data(draft)
        for field in _ADULT_STEPS if audience == "adult" else _CHILD_STEPS:
            if data.get(field) in (None, ""):
                return field
        return "confirm" if draft.get("group_id") else "slot"

    @_bot_scoped
    def _evaluate_and_respond(
        self,
        chat_id: str,
        bot_name: str,
        draft: dict,
        lang: str,
        signup_actor: str | None = None,
        user_text: str | None = None,
        history: list | None = None,
        wanted: bool = True,
        picking: bool = False,
    ) -> str:
        """The reply for the draft's first missing step.

        At the class step a «2» from the list (`picking`) or a wish just made
        (`wanted`; an edit of a confirmed trial carries one in the draft) that
        fits one class picks it; otherwise the list is shown, narrowed to what
        the client wishes for.
        """
        step = self._next_step(bot_name, draft)
        if step == "audience":
            return _audience_prompt(lang, draft.get("signup_for"))
        adult = _stored_audience(draft) == "adult"
        signup_actor = self._signup_actor(draft)
        _current_actor.set(signup_actor)
        if step not in ("slot", "confirm"):
            return _ask_missing(lang, step, signup_actor, history)

        slots, note = self._class_slots(bot_name, draft), None
        if not slots:
            return self._no_classes(bot_name, draft, lang, signup_actor)
        if step == "confirm":
            if _slot_on_offer(draft, slots):
                return self._summary(draft, slots, lang, adult)
            result = trial_service.update_intake(bot_name, draft["id"], {**_CLEARED_SLOT, "language": lang})
            if not result["ok"]:
                return result.get("message") or _loc(lang, "no_groups")
            draft = result["data"]["trial"]

        picked = _picked_from_list(user_text, history, slots, adult) if picking else None
        wish = _wish(draft)
        shown, missed = _classes_on_offer(slots, wish)
        if missed:
            note = _loc(lang, "adult_unavailable" if adult else "preferred_unavailable")
        elif not picked and wanted and any(wish.values()) and len(shown) == 1:
            picked = shown[0]
        if picked:
            result = trial_service.assign_slot(bot_name, draft["id"], picked)
            if not result["ok"]:
                return result.get("message") or _loc(lang, "no_groups")
            return self._summary(result["data"]["trial"], slots, lang, adult)
        reply = _class_list(shown, lang, adult, narrowed=any(wish.values()) and not missed)
        return f"{note}\n\n{reply}" if note else reply

    def _class_slots(self, bot_name: str, draft: dict) -> list[dict]:
        """The classes this signup can pick from, by date and time. Empty
        until the intake is done."""
        if self._next_step(bot_name, draft) not in ("slot", "confirm"):
            return []
        if _stored_audience(draft) == "adult":
            slots = trial_logic.get_adult_trial_slots(bot_name)
        else:
            slots = trial_logic.get_eligible_trial_slots(
                bot_name, int(draft["child_birth_year"]), draft["school_shift"], experience=draft.get("experience"),
            )
        return sorted(slots, key=lambda s: (_iso_date(s["date"]), _hhmm(s["time_start"])))

    @staticmethod
    def _summary(draft: dict, slots: list[dict], lang: str, adult: bool) -> str:
        trainer = _slot_trainer(draft, slots)
        return _adult_confirmation(draft, lang, trainer) if adult else _confirmation(draft, lang, trainer)

    def _no_classes(self, bot_name: str, draft: dict, lang: str, signup_actor: str | None) -> str:
        if _stored_audience(draft) == "adult":
            return _loc(lang, "no_groups_call_admin")
        age_slots = trial_logic.get_birth_year_trial_slots(bot_name, int(draft["child_birth_year"]))
        return _no_groups_with_age_options(lang, age_slots, draft, signup_actor)

    def _age_no_group(self, bot_name: str, age: int, lang: str) -> str:
        unit = "лет" if lang == "ru" else "жас"
        ranges = [("детская группа" if lang == "ru" else "балалар тобы") + f" — {CHILD_AGES[0]}–{CHILD_AGES[1]} {unit}"]
        if self._offers_adult_group(bot_name):
            ranges.append(("взрослая" if lang == "ru" else "ересектер тобы") + f" — {ADULT_AGES[0]}–{ADULT_AGES[1]} {unit}")
        return _loc(lang, "age_no_group", age=age, ranges=", ".join(ranges),
                    phone=ACADEMY_ADMIN_PHONES.get(bot_name, ""))

    def _confirm(self, chat_id: str, bot_name: str, draft: dict, lang: str) -> str:
        """Confirm the draft's chosen class, re-checking it still fits first."""
        if not trial_logic.is_trial_slot_eligible(bot_name, draft):
            result = trial_service.update_intake(
                bot_name, draft["id"],
                {**_CLEARED_SLOT, **dict.fromkeys(_wish({})), "language": lang},
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
        group = academy_repo.get_group_by_id(int(trial["group_id"])) if trial.get("group_id") else None
        trainer = (group or {}).get("trainer")
        if trial.get("audience") == "adult":
            body = _with_trainer(_loc(
                lang,
                "confirmed_adult",
                date=_fmt_date(trial["trial_day"], lang),
                start=str(trial["start_time"])[:5],
                name=trial.get("child_name", ""),
            ), trainer, lang)
        else:
            body = _with_trainer(_loc(
                lang,
                "confirmed",
                date=_fmt_date(trial["trial_day"], lang),
                start=str(trial["start_time"])[:5],
                end=str(trial["end_time"])[:5],
                name=trial.get("child_name", ""),
            ), trainer, lang)
        # Every signup ends with what to bring to the first class.
        body = "\n\n".join(part for part in (body, academy_what_to_bring(bot_name, lang)) if part)
        location = academy_location(bot_name, lang)
        return f"{body}\n\n{location}" if location else body

    @staticmethod
    def _offers_adult_group(bot_name: str) -> bool:
        return bool(academy_repo.get_groups_info(bot_name=bot_name, audience="adult"))

    # ══════════════════════════════════════════════════════════════════════
    #  Read-only views for the message handler
    # ══════════════════════════════════════════════════════════════════════

    @_bot_scoped
    def pending_prompt(
        self, bot_name: str, draft: dict | None, lang: str,
    ) -> str | None:
        """The question the flow is waiting on, worked out from the draft.

        Read-only — nothing is assigned or cleared — so it can be shown after
        a side answer and handed to the intent router.
        """
        if not draft:
            return None
        step = self._next_step(bot_name, draft)
        if step == "audience":
            return _audience_prompt(lang, draft.get("signup_for"))
        adult = _stored_audience(draft) == "adult"
        signup_actor = self._signup_actor(draft)
        token = _current_actor.set(signup_actor)
        try:
            if step not in ("slot", "confirm"):
                return _ask_missing(lang, step, signup_actor)
            slots = self._class_slots(bot_name, draft)
            if not slots:
                return None
            if step == "confirm" and _slot_on_offer(draft, slots):
                return _loc(lang, "confirm_or_questions")
            wish = _wish(draft)
            shown, missed = _classes_on_offer(slots, wish)
            return _class_list(shown, lang, adult, narrowed=any(wish.values()) and not missed)
        finally:
            _current_actor.reset(token)

    @_bot_scoped
    @_bot_scoped
    def choosing_class(self, bot_name: str, draft: dict | None) -> bool:
        """The client is at the class list."""
        return bool(draft) and self._next_step(bot_name, draft) == "slot"

    @_bot_scoped
    def fitting_slots_context(self, bot_name: str, draft: dict | None) -> str | None:
        """The classes this signup can actually be booked into, for the LLM to
        answer day/time questions mid-signup — instead of the general schedule,
        which mixes every trainer and age group. None until the intake is done."""
        if not draft:
            return None
        slots = self._class_slots(bot_name, draft)
        if not slots:
            return None
        lines = [
            f"  {_fmt_date(s['date'], 'ru')} {_hhmm(s['time_start'])}–{_hhmm(s['time_end'])}"
            f" — {s.get('group_name') or 'группа'}{_trainer_suffix(s)}"
            for s in slots
        ]
        return (
            "Занятия, на которые можно записать этого клиента (возраст, уровень и смена уже учтены). "
            "На вопросы о днях и времени пробного отвечай ТОЛЬКО по этому списку — "
            "других подходящих вариантов нет:\n" + "\n".join(lines)
        )
