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
from chat.system_prompts.academy_rules import ACADEMY_ADMIN_PHONES
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
        "ru": "Какая школьная смена у ребенка?\n1. Утренняя\n2. Дневная\n3. Ходит в детский сад",
        "kk": "Балаңыз қай ауысымда оқиды?\n1. Таңғы\n2. Түскі\n3. Балабақшаға барады",
    },
    "confirm_self": {
        "ru": "📋 Детали записи:\n📅 {date}\n⏰ {start}–{end}\n👤 Ваше имя: {name}\n📆 Год рождения: {birth_year}\n🎯 Опыт: {experience}\n🏫 Смена: {school_shift}\n\nПодтвердить?",
        "kk": "📋 Жазылым деректері:\n📅 {date}\n⏰ {start}–{end}\n👤 Есіміңіз: {name}\n📆 Туған жылы: {birth_year}\n🎯 Тәжірибе: {experience}\n🏫 Ауысым: {school_shift}\n\nРастайсыз ба?",
    },
    "confirmed_self": {
        "ru": "Вы записаны на пробный урок, будем вас ждать!\n📅 {date}\n⏰ {start}–{end}\n👤 Ваше имя: {name}",
        "kk": "Жазылым сәтті аяқталды, сізді күтеміз!\n📅 {date}\n⏰ {start}–{end}\n👤 Есіміңіз: {name}",
    },
    "ask_birth_year_self": {
        "ru": "Укажите ваш возраст или год рождения. Например: *2010*.",
        "kk": "Жасыңызды немесе туған жылыңызды жазыңыз. Мысалы: *2010*.",
    },
    "ask_experience_self": {
        "ru": "Выберите ваш уровень подготовки:\n1. Начинающий\n2. Средний\n3. Продвинутый",
        "kk": "Дайындық деңгейіңізді таңдаңыз:\n1. Бастапқы\n2. Орта\n3. Жоғары",
    },
    "ask_school_shift_self": {
        "ru": "В какую смену вы учитесь?\n1. Утренняя\n2. Дневная",
        "kk": "Қай ауысымда оқисыз?\n1. Таңғы\n2. Түскі",
    },
    # One clarification of an unclear level answer («бұрын қатысқанмын»);
    # the numbering matches ask_experience, so «2» still means Intermediate.
    "experience_clarify": {
        "ru": "Уточните, пожалуйста, сколько ребёнок занимался:\n1. Не занимался\n2. До года\n3. Больше года",
        "kk": "Нақтылап жіберіңізші, бала қанша уақыт айналысты:\n1. Айналыспаған\n2. Бір жылға дейін\n3. Бір жылдан көп",
    },
    "experience_clarify_self": {
        "ru": "Уточните, пожалуйста, сколько вы занимались:\n1. Не занимался(-ась)\n2. До года\n3. Больше года",
        "kk": "Нақтылап жіберіңізші, қанша уақыт айналыстыңыз:\n1. Айналыспағанмын\n2. Бір жылға дейін\n3. Бір жылдан көп",
    },
    "beginner_note": {
        "ru": "Ничего страшного — начинающих мы тоже берём.",
        "kk": "Ештеңе етпейді — жаңадан бастаушыларды да қабылдаймыз.",
    },
    # Put in front of the first reply of a new signup. Empty: the football
    # bot's material doesn't say its trial is free.
    "trial_free_intro": {
        "ru": "",
        "kk": "",
    },
    # A new signup prefilled from an abandoned one (academy_trial_summaries).
    "restored_answers": {
        "ru": "Я сохранил ваши прошлые ответы: {details}. Если что-то изменилось — напишите.",
        "kk": "Алдыңғы жауаптарыңызды сақтап қойдым: {details}. Бірдеңе өзгерсе — жазыңыз.",
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
    # Asked before any signup question on a bot that has an adult group.
    # The numbering must stay in sync with _AUDIENCE_CHOICES.
    "choose_audience": {
        "ru": "Выберите нужную группу:\n1. Детская группа {min_age}–{max_age} лет\n2. Взрослая группа от 18 лет",
        "kk": "Керек топты таңдаңыз:\n1. {min_age}-{max_age} жас балалар тобы\n2. 18 жастан бастап ересектер тобы",
    },
    # An age between the children's groups and the adult group (e.g. 17).
    "age_no_group": {
        "ru": "К сожалению, для возраста {age} лет группы нет: детские группы — {min_age}–{max_age} лет, "
              "взрослая — от 18 лет. Администратор подскажет вариант: {phone}.",
        "kk": "Өкінішке қарай, {age} жасқа арналған топ жоқ: балалар тобы — {min_age}-{max_age} жас, "
              "ересектер тобы — 18 жастан. Әкімші нұсқа ұсынады: {phone}.",
    },
    "adult_schedule": {
        "ru": "Тренировки взрослой группы:\n{lines}\n\nВ какой день вам удобно прийти на пробное занятие?",
        "kk": "Ересектер тобының жаттығулары:\n{lines}\n\nСынақ сабағына қай күні келу сізге ыңғайлы?",
    },
    "adult_unavailable": {
        "ru": "В это время тренировки взрослой группы нет.",
        "kk": "Бұл уақытта ересектер тобының жаттығуы жоқ.",
    },
    "adult_no_other_times": {
        "ru": "Других тренировок у взрослой группы сейчас нет — только время ниже. "
              "Если оно не подходит, администратор подскажет варианты: {phone}.",
        "kk": "Ересектер тобының басқа жаттығу уақыты қазір жоқ — тек төмендегі уақыттар. "
              "Егер келмесе, әкімші басқа нұсқаны айтып береді: {phone}.",
    },
    "confirm_adult": {
        "ru": "📋 Детали записи:\n👥 Взрослая группа\n📅 {date}\n⏰ {start}\n👤 Имя: {name}\n\nВсё верно? Подтверждаете запись?",
        "kk": "📋 Жазылым деректері:\n👥 Ересектер тобы\n📅 {date}\n⏰ {start}\n👤 Есіміңіз: {name}\n\nБәрі дұрыс па? Жазылымды растайсыз ба?",
    },
    "confirmed_adult": {
        "ru": "Готово, вы записаны на пробное занятие! 🎉 Будем рады вас видеть!\n👥 Взрослая группа\n📅 {date}\n⏰ {start}\n👤 Имя: {name}",
        "kk": "Дайын, сынақ сабағына жазылдыңыз! 🎉 Сізді күтеміз!\n👥 Ересектер тобы\n📅 {date}\n⏰ {start}\n👤 Есіміңіз: {name}",
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
        "ru": "К сожалению, этот возраст нам не подходит ({year} год рождения). Мы принимаем детей от {min_age} до {max_age} лет. "
              "Подскажите, пожалуйста, сколько лет ребёнку?",
        "kk": "Өкінішке қарай, бұл жас сәйкес келмейді ({year} туған жыл). Біз {min_age} мен {max_age} жас аралығындағы балаларды қабылдаймыз. "
              "Балаңыз неше жаста?",
    },
    "name_too_long": {
        "ru": "Имя получилось немного длинным. Пожалуйста, напишите его покороче — до {max_len} символов.",
        "kk": "Есім сәл ұзын болып кетті. {max_len} таңбаға дейін қысқартып жазыңызшы.",
    },
    "ask_birth_year": {
        "ru": "Подскажите, пожалуйста, сколько лет ребёнку? Можно написать и год рождения, например: 2016.",
        "kk": "Балаңыз неше жаста? Туған жылын да жазуға болады, мысалы: 2016.",
    },
    "ask_experience": {
        "ru": "Какой у ребёнка уровень подготовки? Выберите, пожалуйста:\n1. Начинающий\n2. Средний\n3. Продвинутый\n\n"
              "Если ребёнок раньше не занимался боксом — смело выбирайте «Начинающий» 🥊",
        "kk": "Балаңыздың дайындық деңгейі қандай? Таңдаңызшы:\n1. Бастапқы\n2. Орта\n3. Жоғары\n\n"
              "Егер бала бұрын бокспен айналыспаса — «Бастапқы» деңгейін таңдаңыз 🥊",
    },
    "ask_school_shift": {
        "ru": "В какую смену ребёнок учится в школе? Так мы подберём удобное время.\n1. Утренняя\n2. Дневная\n3. Ходит в детский сад",
        "kk": "Балаңыз мектепте қай ауысымда оқиды? Ыңғайлы уақытты таңдау үшін керек.\n1. Таңғы\n2. Түскі\n3. Балабақшаға барады",
    },
    "confirm_self": {
        "ru": "📋 Детали записи:\n📅 {date}\n⏰ {start}–{end}\n👤 Ваше имя: {name}\n📆 Год рождения: {birth_year}\n🎯 Опыт: {experience}\n🏫 Смена: {school_shift}\n\nВсё верно? Подтверждаете запись?",
        "kk": "📋 Жазылым деректері:\n📅 {date}\n⏰ {start}–{end}\n👤 Есіміңіз: {name}\n📆 Туған жылы: {birth_year}\n🎯 Тәжірибе: {experience}\n🏫 Ауысым: {school_shift}\n\nБәрі дұрыс па? Жазылымды растайсыз ба?",
    },
    "confirmed_self": {
        "ru": "Готово, вы записаны на пробное занятие! 🎉 Будем очень рады вас видеть!\n📅 {date}\n⏰ {start}–{end}\n👤 Ваше имя: {name}",
        "kk": "Дайын, сынақ сабағына жазылдыңыз! 🎉 Сізді асыға күтеміз!\n📅 {date}\n⏰ {start}–{end}\n👤 Есіміңіз: {name}",
    },
    "ask_birth_year_self": {
        "ru": "Подскажите, пожалуйста, сколько вам лет?",
        "kk": "Жасыңыз нешеде?",
    },
    "ask_experience_self": {
        "ru": "Какой у вас уровень подготовки? Выберите, пожалуйста:\n1. Начинающий\n2. Средний\n3. Продвинутый\n\n"
              "Если раньше не занимались боксом — смело выбирайте «Начинающий» 🥊",
        "kk": "Дайындық деңгейіңіз қандай? Таңдаңызшы:\n1. Бастапқы\n2. Орта\n3. Жоғары\n\n"
              "Бұрын бокспен айналыспасаңыз — «Бастапқы» деңгейін таңдаңыз 🥊",
    },
    "ask_school_shift_self": {
        "ru": "В какую смену вы учитесь? Так мы подберём удобное время.\n1. Утренняя\n2. Дневная",
        "kk": "Мектепте қай ауысымда оқисыз? Ыңғайлы уақытты таңдау үшін керек.\n1. Таңғы\n2. Түскі",
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


# "self" while the client is signing up themselves — _loc then picks a key's
# "_self" variant («Есіміңіз» rather than «Балаңыздың есімі») where one exists.
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
        "kindergarten": "детский сад",
    },
    "kk": {
        "morning": "таңғы",
        "afternoon": "түскі",
        "kindergarten": "балабақша",
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
    "3": "kindergarten",
    "kindergarten": "kindergarten",
}

# Free-text shift answers. Kindergarten is checked first — «балабақшаға
# барады, түстен кейін үйде» is a kindergarten child, not an afternoon shift.
_KINDERGARTEN_RE = re.compile(r"бала\s?бақша|балабакша|детск\w*\s+сад|детсад|садик|\bв\s+сад\b", re.IGNORECASE)
_MORNING_SHIFT_RE = re.compile(
    r"утрен|утром|таң|бірінші|биринши|бiрiншi|перв\w*\s+смен|\b1\s*-?\s*(?:ші|шы|-?я)?\s*(?:ауысым|смен)",
    re.IGNORECASE,
)
_AFTERNOON_SHIFT_RE = re.compile(
    r"дневн|дн[её]м|түск|түстен|тустен|екінші|екинши|втор\w*\s+смен|после\s+обеда|"
    r"\b2\s*-?\s*(?:ші|шы|-?я)?\s*(?:ауысым|смен)",
    re.IGNORECASE,
)


def shift_from_text(text: str | None) -> str | None:
    """'kindergarten' / 'morning' / 'afternoon' from a free-text answer:
    «бірінші ауысым», «екінші ауысымда оқимын», «түстен кейін», «балабақшаға
    барады», «вторая смена», «ходит в садик»."""
    low = (text or "").lower()
    if _KINDERGARTEN_RE.search(low):
        return "kindergarten"
    if _AFTERNOON_SHIFT_RE.search(low):
        return "afternoon"
    if _MORNING_SHIFT_RE.search(low):
        return "morning"
    return None

# «бұрын қатыспаған», «с нуля» — a beginner. «бұрын қатысқанмын» says
# there was training but not how much: asked once (experience_clarify).
_NEVER_TRAINED_RE = re.compile(
    r"қатыспаға|айналыспаға|қатыспады|айналыспады|нөлден|с нуля|не занимал|не ходил|никогда не",
    re.IGNORECASE,
)
_TRAINED_BEFORE_RE = re.compile(r"қатысқан|айналысқан|занимал|ходил", re.IGNORECASE)
_CLARIFY_MARKERS = ("1. Айналыспаға", "1. Не занимал")

_YES = {"да", "иә", "ия", "иа", "ok", "ок", "подтверждаю", "yes", "жарайды", "дұрыс", "дурыс", "растаймын"}
_NO = {"нет", "жоқ", "жок", "no", "отмена", "бас тартамын", "бас тарту"}


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
    # Kazakh greetings, plus the spellings a Russian keyboard produces.
    "салем", "сәлеметсіз бе", "салеметсиз бе", "сәлеметсіздер ме", "салеметсиздер ме",
    "саламатсыз ба", "саламатсыздар ма",
    "ассалаумағалейкум", "ассалаумагалейкум", "ассалаумалейкум", "ассаламуалейкум",
    "ассалам алейкум", "салам алейкум",
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
    r"\b(?:мне|маған|жасым)\s+(\d{1,2})\s*(?:лет|жас\w*|-?[тд][еа])?\b"
    r"|\b(\d{1,2})\s*(?:лет|жас)(?:\s+мне|\s+маған)?\b",
    re.IGNORECASE,
)
_SELF_SIGNUP_RE = re.compile(
    r"\b(?:мне|я\s+хочу|хочу\s+записаться|хочу\s+заниматься|маған|өзім|мен\s+қатысамын|жасым)\b",
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

# ── Audience: children's group or the adult group ──
# From this age the boxing student goes to the adult group.
ADULT_MIN_AGE = 18
# An age this high in a children's signup can be the parent's own («мне 35
# лет, хочу записать сына»), never the child's.
PARENT_MIN_AGE = 19
_AUDIENCE_CHOICES = {"1": "child", "2": "adult"}
# Asking for the adult group in words — wins over everything else.
_ADULT_GROUP_RE = re.compile(
    r"ересек\w*|взросл\w*|для\s+себя|өзім\s+үшін|өзіме|18\s*\+|старше\s+1[78]",
    re.IGNORECASE,
)
_CHILD_GROUP_RE = re.compile(r"детск\w*|балалар\s+тоб\w*", re.IGNORECASE)
# «25 жастамын», «мен 30 жаста», «мне 25 лет». A range («10-18 жас») is
# stripped first — it describes a group, not a person.
# «бұл уақыт маған келмейді», «не подходит», «другое время есть?» — the
# offered schedule doesn't suit the client.
_SCHEDULE_DOESNT_FIT_RE = re.compile(
    r"келмейді|келмейд|ыңғайсыз|сәйкес емес|басқа уақыт|"
    r"не подход|не устраива|неудобн|не получится|не смогу|друго[ей] врем",
    re.IGNORECASE,
)
_PERSON_AGE_RE = re.compile(r"\b(\d{1,2})\s*(?:лет|года?|жас\w*)", re.IGNORECASE)
_AGE_RANGE_RE = re.compile(r"\d{1,2}\s*[-–—]\s*\d{1,2}")


def _loc(lang: str, key: str, **fmt) -> str:
    table = _BOT_TEXTS.get(_current_bot.get(), {})
    if _current_actor.get() == "self" and f"{key}_self" in T:
        key = f"{key}_self"
    entry = table.get(key) or T[key]
    text = entry.get(lang) or entry["ru"]
    return text.format(**fmt) if fmt else text


def _birth_year_from_age(text: str, today: date | None = None) -> int | None:
    today = today or today_almaty()
    # _ages_in also reads «8 жаста» / «16 жастамын», which _AGE_RE misses.
    for age in _ages_in(text):
        # The children's range check is _birth_year_rejection's: an age just
        # outside it (a 16- or 17-year-old) still reaches it, so the client is
        # told why instead of being asked again. 18+ is left to
        # _detect_audience — it may be a parent's own age («мне 35 лет,
        # хочу записать сына»), never the child's birth year.
        if 3 <= age < PARENT_MIN_AGE:
            return today.year - age
        _log_rejected("child_age", "implausible", age)
    return None


def _ages_in(text: str | None) -> list[int]:
    """Ages a person states about themselves or a child — «25 жастамын»,
    «мне 30 лет», «12 жаста». Ranges and «до 18 лет» / «18 жасқа дейін» are
    limits, not ages, and are skipped."""
    text = _AGE_RANGE_RE.sub(" ", (text or "").lower())
    ages = []
    for match in _PERSON_AGE_RE.finditer(text):
        before, after = text[max(0, match.start() - 3):match.start()], text[match.end():match.end() + 7]
        if "до " in before or "дейін" in after:
            continue
        ages.append(int(match.group(1)))
    ages.extend(int(m.group(1)) for m in _AGE_RE.finditer(text) if m.group(1))
    return ages


def _detect_audience(text: str | None, birth_year=None, today: date | None = None) -> str | None:
    """'adult' / 'child' when the message says which group it is about, else None.

    Order: an explicit group («ересектер тобы», «для себя») wins; then a child
    mentioned («балам», «сын») — a parent stating their own age is still
    signing up a child; then an age or birth year.
    """
    low = _NOT_A_CHILD_RE.sub(" ", (text or "").lower())
    if _ADULT_GROUP_RE.search(low):
        return "adult"
    if _CHILD_GROUP_RE.search(low) or _CHILD_SUBJECT_RE.search(low):
        return "child"
    ages = [a for a in _ages_in(low) if 3 <= a <= 99]
    try:
        if birth_year not in (None, ""):
            ages.append((today or today_almaty()).year - int(birth_year))
    except (TypeError, ValueError):
        pass
    if any(a >= ADULT_MIN_AGE for a in ages):
        return "adult"
    if any(3 <= a < ADULT_MIN_AGE for a in ages):
        return "child"
    return None


def _audience_choice(text: str | None) -> str | None:
    """The reply to the choose_audience list: «1» / «2» or the group in words."""
    low = (text or "").strip().strip(".,)!").lower()
    return _AUDIENCE_CHOICES.get(low) or _detect_audience(low)


def _audience_in_history(history: list) -> str | None:
    """The group named in an earlier client message — «ересектерге бокс бар
    ма?» followed by «жазылғым келеді». Newest message first."""
    for message in reversed(history or []):
        if message.get("role") == "user":
            found = _detect_audience(message.get("content"))
            if found:
                return found
    return None


def _stored_audience(draft: dict) -> str | None:
    """The draft's group. A draft from before the group choice existed that
    already holds a child's birth year is a children's signup."""
    if draft.get("audience") in ("child", "adult"):
        return draft["audience"]
    if draft.get("child_birth_year") not in (None, ""):
        return "child"
    return None


def _waiting_field(draft: dict, audience: str | None) -> str | None:
    """The question the flow is waiting on: the group, or a missing field.

    An adult picks the class first and is asked the name after it, so the
    name is only "being answered" once a class is chosen — before that a «3»
    is a pick from the list, not a name.
    """
    if audience is None:
        return "audience"
    if audience == "adult":
        if draft.get("group_id") and not _is_plausible_child_name(draft.get("child_name")):
            return "child_name"
        return None
    return _missing_prerequisite(_draft_to_data(draft))


# «балам емес, өзім қатысамын», «не для ребёнка» — a child named only to say
# it is not one.
_NOT_A_CHILD_RE = re.compile(
    r"\b(?:бала\w*|ұлым\w*|қызым\w*)\s+емес\b|\bне\s+(?:для\s+)?(?:реб[её]н\w*|сын\w*|доч\w*|дет\w*)",
    re.IGNORECASE,
)


def _signup_actor(text: str | None) -> str | None:
    text = _NOT_A_CHILD_RE.sub(" ", text or "")
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
                              allowed_age="{}-{}".format(*_child_age_limits()))
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


def _draft_idle(draft: dict) -> timedelta | None:
    updated_at = draft.get("updated_at")
    if not isinstance(updated_at, datetime):
        return None
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - updated_at


def is_draft_in_progress(draft: dict) -> bool:
    """True while the client is still filling this draft in: a reply within
    TRIAL_IN_PROGRESS_SECONDS answers the last question. Later the draft's
    data is still reused, until it is abandoned (is_draft_abandoned)."""
    idle = _draft_idle(draft)
    return idle is None or idle <= timedelta(seconds=config.TRIAL_IN_PROGRESS_SECONDS)


def is_draft_abandoned(draft: dict) -> bool:
    """True once nobody touched the draft for TRIAL_DRAFT_KEEP_SECONDS — it is
    then cancelled and its answers kept as a summary (abandon_draft)."""
    idle = _draft_idle(draft)
    return idle is not None and idle > timedelta(seconds=config.TRIAL_DRAFT_KEEP_SECONDS)


def _restored_details(answers: dict, lang: str) -> str:
    parts = [answers.get("child_name")]
    if answers.get("child_birth_year"):
        parts.append(f"{answers['child_birth_year']} {'г.р.' if lang == 'ru' else 'ж.т.'}")
    parts += [_level_label(answers.get("experience"), lang), _shift_label(answers.get("school_shift"), lang)]
    return ", ".join(str(p) for p in parts if p)


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
        if _NEVER_TRAINED_RE.search(low):
            return "Beginner"
        if "средн" in low or "орта" in low:
            return "Intermediate"
        if "продвин" in low or "жоғары" in low or "жетілген" in low:
            return "Advanced"
        return None
    if field == "school_shift":
        return _SHIFT_ALIASES.get(low) or shift_from_text(low)
    if field == "child_birth_year":
        parts = low.replace(",", " ").replace(".", " ").split()
        years = [int(part) for part in parts if part.isdigit() and len(part) == 4]
        if years:
            return years[-1]
        # A bare age in reply to «неше жаста?» — «8», «8 жас»; the range is
        # _birth_year_rejection's to check.
        ages = [int(part) for part in parts if part.isdigit() and len(part) <= 2]
        if len(ages) == 1:
            return today_almaty().year - ages[0]
        return None
    if field == "child_name":
        name = _strip_name_lead(text)
        if not _is_plausible_child_name(name):
            return None
        return name or None
    return None


# academy_trials.child_name is VARCHAR(30); a longer value fails the UPDATE.
CHILD_NAME_MAX_LEN = trial_service.TRIAL_TEXT_LIMITS["child_name"]
# Fallback age window, used only when the bot's children's groups list no
# birth years (or no bot is in context). Otherwise _child_age_limits takes
# the window from the groups.
CHILD_MIN_AGE = 5
CHILD_MAX_AGE = 18


def _child_age_limits(today: date | None = None) -> tuple[int, int]:
    """(youngest, oldest) child age this bot's children's groups take."""
    bot_name = _current_bot.get()
    found = trial_logic.child_age_range(bot_name, today) if bot_name else None
    min_age, max_age = found or (CHILD_MIN_AGE, CHILD_MAX_AGE)
    if bot_name and academy_repo.get_groups_info(bot_name=bot_name, audience="adult"):
        max_age = min(max_age, ADULT_MIN_AGE - 1)
    return min_age, max_age


def _audience_prompt(lang: str) -> str:
    min_age, max_age = _child_age_limits()
    return _loc(lang, "choose_audience", min_age=min_age, max_age=max_age)


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
    min_age, max_age = _child_age_limits(today)
    if current - year < min_age:
        return "too_young"
    if current - year > max_age:
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
                          allowed_age="{}-{}".format(*_child_age_limits()))
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
        "емес", "қатыс", "өзім", "жасым", "деңгей",
    )
    if any(fragment in lowered for fragment in blocked_fragments):
        return "blocked_phrase"
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


def is_greeting(text: str) -> bool:
    normalized = " ".join(re.sub(r"[!.?,]", " ", (text or "").lower()).split())
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


def _waiting_prompt(lang: str, waiting_for: str | None, signup_actor: str | None = None) -> str:
    if not waiting_for:
        return ""
    prompt = _ask_missing(lang, waiting_for, signup_actor)
    if lang == "ru":
        return f"\n\nЧтобы продолжить запись на пробное занятие: {prompt}"
    return f"\n\nСынақ сабағына жазылуды жалғастыру үшін: {prompt}"


def _session_interrupt_response(
    lang: str, text: str, waiting_for: str | None = None, signup_actor: str | None = None,
) -> str | None:
    if is_bot_identity_question(text):
        base = (
            "Я бот-ассистент академии. Могу ответить на вопросы о тренировках "
            "и помочь записаться на пробное занятие."
            if lang == "ru"
            else "Мен академияның бот-ассистентімін. Жаттығулар туралы сұрақтарға "
                 "жауап беріп, сынақ сабағына жазуға көмектесемін."
        )
        return base + _waiting_prompt(lang, waiting_for, signup_actor)

    if is_greeting(text):
        base = "Здравствуйте!" if lang == "ru" else "Сәлеметсіз бе!"
        return base + _waiting_prompt(lang, waiting_for, signup_actor)

    if is_acknowledgement(text):
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


def _merge(base: dict, patch: dict) -> dict:
    merged = dict(base)
    for key, value in patch.items():
        if value not in (None, ""):
            merged[key] = value
    return merged


_INTAKE_ORDER = ("child_name", "child_birth_year", "experience", "school_shift")
# Boxing asks the age first: it is what tells whether a group fits, so a
# parent hears it before giving the rest. Football keeps its order.
_BOT_INTAKE_ORDER = {
    "dopsy_boxing": ("child_birth_year", "child_name", "experience", "school_shift"),
}


def _missing_prerequisite(data: dict) -> str | None:
    for key in _BOT_INTAKE_ORDER.get(_current_bot.get(), _INTAKE_ORDER):
        if data.get(key) in (None, ""):
            return key
    return None


def _ask_missing(lang: str, field: str, signup_actor: str | None = None) -> str:
    if field == "audience":
        return _audience_prompt(lang)
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
    return body + _yes_no_hint(lang)


def _yes_no_hint(lang: str) -> str:
    # The boxing bot writes plain text, no *bold*.
    if _current_bot.get() == "dopsy_boxing":
        return "\n\nОтветьте «да» или «нет»." if lang == "ru" else "\n\n«Иә» немесе «жоқ» деп жауап беріңіз."
    return "\n\nОтветьте *да* или *нет*." if lang == "ru" else "\n\n*Иә* немесе *жоқ* деп жауап беріңіз."


def _adult_slot_lines(slots: list[dict], lang: str) -> str:
    """«1. Сс 20.10.2026 — 19:15». The adult group's end time is technical
    (time_end is NOT NULL) and is never shown."""
    ordered = sorted(slots, key=lambda s: (str(s["date"]), _hhmm(s["time_start"])))
    return "\n".join(
        f"{i}. {_fmt_date(s['date'], lang)} — {_hhmm(s['time_start'])}"
        for i, s in enumerate(ordered, 1)
    )


def _narrow_adult(
    slots: list[dict], draft: dict, lang: str, pick: bool = True, note_unavailable: bool = True,
) -> tuple[dict | None, str]:
    """_narrow for the adult group: a handful of classes, so the whole
    schedule is listed with times at once instead of days first.

    A weekday whose class has already started today («сенбі» on Saturday
    afternoon) means that weekday's next class.
    """
    wanted_day = _iso_date(draft.get("preferred_date"))
    wanted_start = _hhmm(draft.get("preferred_time_start"))
    if wanted_day and wanted_day < today_almaty().isoformat():
        wanted_day = None

    def on_wanted_day(slot: dict) -> bool:
        day = _iso_date(slot["date"])
        if day == wanted_day:
            return True
        return (wanted_day not in {_iso_date(s["date"]) for s in slots}
                and date.fromisoformat(day).weekday() == date.fromisoformat(wanted_day).weekday())

    matches = [
        s for s in slots
        if (not wanted_day or on_wanted_day(s))
        and (not wanted_start or _hhmm(s["time_start"]) == wanted_start)
    ]
    asked = bool(wanted_day or wanted_start)
    if pick and asked and len(matches) == 1:
        return matches[0], ""
    body = _loc(lang, "adult_schedule", lines=_adult_slot_lines(matches or slots, lang))
    if asked and not matches and note_unavailable:
        return None, f"{_loc(lang, 'adult_unavailable')}\n\n{body}"
    return None, body


def _adult_confirmation(draft: dict, lang: str) -> str:
    body = _loc(
        lang,
        "confirm_adult",
        date=_fmt_date(draft["trial_day"], lang),
        start=str(draft["start_time"])[:5],
        name=draft.get("child_name", ""),
    )
    return body + _yes_no_hint(lang)


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
        self._new_draft = False
        self._restored_note = ""
        token = _current_actor.set(_signup_actor_in(history, user_text))
        try:
            reply = self._handle(chat_id, sender_phone, bot_name, user_text, history, lang)
        finally:
            _current_actor.reset(token)
        # A new signup opens with "the trial is free", then the first question.
        intro = _loc(lang, "trial_free_intro") if self._new_draft else ""
        return "\n\n".join(part for part in (intro, self._restored_note, reply) if part)

    def _handle(
        self,
        chat_id: str,
        sender_phone: str,
        bot_name: str,
        user_text: str,
        history: list,
        lang: str,
    ) -> str:
        # "какие у меня пробные" / "мои занятия" — answered before touching any
        # draft, so asking about an existing signup never creates an empty one.
        if _is_my_trial_query(user_text):
            from handlers.edit_trial import handle_trial_status_request
            return handle_trial_status_request(sender_phone, bot_name, lang)

        draft = academy_repo.get_existing_trial_draft(sender_phone, bot_name)
        if draft and is_draft_abandoned(draft):
            trial_service.abandon_draft(bot_name, chat_id, draft)
            draft, history = None, []

        # ── 1. A draft with a chosen class: yes/no confirms or cancels it ──
        if draft and draft.get("group_id") and not _waiting_field(draft, _stored_audience(draft)):
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
            self._new_draft = True
            draft = self._restore_answers(bot_name, sender_phone, draft, lang)

        if draft.get("child_name") and not _is_plausible_child_name(draft.get("child_name")):
            result = trial_service.update_intake(bot_name, draft["id"], {"child_name": None, "language": lang})
            if not result["ok"]:
                return result.get("message") or _loc(lang, "no_groups")
            draft = result["data"]["trial"]

        # Only a bot with an adult group asks which group; elsewhere every
        # signup is a children's one.
        offers_adult = self._offers_adult_group(bot_name)
        audience = _stored_audience(draft) if offers_adult else "child"
        signup_actor = "self" if audience == "adult" else _signup_actor_in(history, user_text)
        waiting_for = _waiting_field(draft, audience)
        interrupt = _session_interrupt_response(lang, user_text, waiting_for, signup_actor)
        if interrupt:
            if waiting_for:
                return interrupt
            return f"{interrupt}\n\n{self._evaluate_and_respond(chat_id, bot_name, draft, lang, signup_actor)}"

        # ── 2. Extract and merge into the draft ──
        field_asked = waiting_for if asked_before and waiting_for != "audience" else None
        extracted, rejected = _extract_user_data(history, user_text, field_asked)
        if (extracted.get("child_name") and draft.get("child_name")
                and _is_trainer_name(bot_name, extracted["child_name"])):
            _log_rejected("child_name", "trainer_name", extracted["child_name"], trial_id=draft["id"])
            extracted["child_name"] = None
        logger.info("[TRIAL] Extracted for id=%d: %s", draft["id"], extracted)

        data = _merge(_draft_to_data(draft), extracted)
        if offers_adult:
            detected = self._detect_audience_change(
                user_text, history, extracted, rejected, audience,
                waiting_for if asked_before else None,
            )
            if detected and detected != audience:
                if audience:
                    # The other group's answers and class don't carry over.
                    logger.info("[TRIAL] Audience %s → %s for id=%d", audience, detected, draft["id"])
                    base = {**_draft_to_data(draft), "child_name": None, "child_birth_year": None,
                            "experience": None, "school_shift": None}
                    data = {**_merge(base, extracted), **_CLEARED_SLOT}
                audience = detected
        if audience:
            data["audience"] = audience
        result = trial_service.update_intake(bot_name, draft["id"], {**data, "language": lang})
        if not result["ok"]:
            return result.get("message") or _loc(lang, "no_groups")
        draft = result["data"]["trial"]

        if audience is None:
            return _audience_prompt(lang)
        if audience == "adult":
            signup_actor = "self"
            _current_actor.set("self")
            if (not draft.get("group_id") and _SCHEDULE_DOESNT_FIT_RE.search(user_text or "")
                    and not (draft.get("preferred_date") or draft.get("preferred_time_start"))):
                note = _loc(lang, "adult_no_other_times", phone=ACADEMY_ADMIN_PHONES.get(bot_name, ""))
                return f"{note}\n\n{self._evaluate_adult(bot_name, draft, lang)}"

        # A value the client gave but the flow refused gets its own message,
        # so the client knows why the same field is asked again.
        missing = _waiting_field(draft, audience)
        reason, bad_value = rejected.get(missing, (None, None))
        if missing == "child_name" and reason == "too_long":
            logger.info("[TRIAL] Asking chat_id=%s for a shorter child_name (max %d chars)",
                        chat_id, CHILD_NAME_MAX_LEN)
            return _loc(lang, "name_too_long", max_len=CHILD_NAME_MAX_LEN)
        # Only a parseable-but-out-of-range year gets this message; a message
        # with no year in it at all is a non-answer, and the field is re-asked.
        if missing == "child_birth_year" and reason in ("in_future", "too_young", "too_old"):
            min_age, max_age = _child_age_limits()
            if reason == "too_old" and offers_adult:
                age = today_almaty().year - int(bad_value)
                if age >= PARENT_MIN_AGE:
                    # Still a children's signup, so an adult's year is the
                    # parent's own — ask for the child's age again.
                    return self._evaluate_and_respond(chat_id, bot_name, draft, lang, signup_actor)
                if age >= ADULT_MIN_AGE:
                    # «балам 18 жаста» — old enough for the adult group.
                    logger.info("[TRIAL] Age %d → adult group for id=%d", age, draft["id"])
                    result = trial_service.update_intake(bot_name, draft["id"], {
                        "audience": "adult", "child_birth_year": None, "experience": None,
                        "school_shift": None, "language": lang})
                    if not result["ok"]:
                        return result.get("message") or _loc(lang, "no_groups")
                    return self._evaluate_adult(bot_name, result["data"]["trial"], lang)
                # Older than the children's groups but under 18: no group fits.
                logger.info("[TRIAL] No group for age %d (children %d-%d, adults %d+) chat_id=%s",
                            age, min_age, max_age, ADULT_MIN_AGE, chat_id)
                return _loc(lang, "age_no_group", age=age, min_age=min_age, max_age=max_age,
                            phone=ACADEMY_ADMIN_PHONES.get(bot_name, ""))
            logger.info("[TRIAL] Asking chat_id=%s for a valid child_birth_year (age %d-%d)",
                        chat_id, min_age, max_age)
            return _loc(lang, "birth_year_invalid", year=bad_value,
                        min_age=min_age, max_age=max_age)

        note = ""
        if field_asked == "experience":
            if missing == "experience" and reason == "unrecognized":
                last_bot = next((m.get("content") or "" for m in reversed(history or [])
                                 if m.get("role") == "assistant"), "")
                if not any(marker in last_bot for marker in _CLARIFY_MARKERS):
                    return _loc(lang, "experience_clarify")
                # Clarified once already: don't ask a third time.
                texts = " ".join(m.get("content") or "" for m in history or [] if m.get("role") == "user")
                level = "Intermediate" if _TRAINED_BEFORE_RE.search(f"{texts} {user_text}") else "Beginner"
                logger.info("[TRIAL] Level still unclear after clarifying → %s id=%d", level, draft["id"])
                result = trial_service.update_intake(bot_name, draft["id"], {"experience": level, "language": lang})
                if not result["ok"]:
                    return result.get("message") or _loc(lang, "no_groups")
                draft = result["data"]["trial"]
            elif draft.get("experience") == "Beginner" and _NEVER_TRAINED_RE.search(user_text or ""):
                note = _loc(lang, "beginner_note")

        # ── 3. Evaluate ──
        reply = self._evaluate_and_respond(chat_id, bot_name, draft, lang, signup_actor)
        return f"{note}\n\n{reply}" if note else reply

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
        if trial.get("audience") == "adult":
            return _loc(
                lang,
                "confirmed_adult",
                date=_fmt_date(trial["trial_day"], lang),
                start=str(trial["start_time"])[:5],
                name=trial.get("child_name", ""),
            )
        return _loc(
            lang,
            "confirmed",
            date=_fmt_date(trial["trial_day"], lang),
            start=str(trial["start_time"])[:5],
            end=str(trial["end_time"])[:5],
            name=trial.get("child_name", ""),
        )

    def _restore_answers(self, bot_name: str, sender_phone: str, draft: dict, lang: str) -> dict:
        """Prefill a new draft with the answers of the client's abandoned one."""
        summary = academy_repo.get_trial_summary(sender_phone, bot_name)
        answers = {f: summary.get(f) for f in academy_repo.SUMMARY_FIELDS
                   if summary and summary.get(f) not in (None, "")}
        if not answers:
            return draft
        result = trial_service.update_intake(bot_name, draft["id"], {**answers, "language": lang})
        if not result["ok"]:
            return draft
        logger.info("[TRIAL] Restored %s into id=%d", sorted(answers), draft["id"])
        details = _restored_details(answers, lang)
        if details:
            self._restored_note = _loc(lang, "restored_answers", details=details)
        return result["data"]["trial"]

    @staticmethod
    def _offers_adult_group(bot_name: str) -> bool:
        return bool(academy_repo.get_groups_info(bot_name=bot_name, audience="adult"))

    @staticmethod
    def _detect_audience_change(
        user_text: str,
        history: list,
        extracted: dict,
        rejected: dict,
        audience: str | None,
        waiting_for: str | None,
    ) -> str | None:
        """The group this message points to, or None when it doesn't say.

        A reply to the name question is not read for markers — «Балауса» would
        otherwise look like «бала». A birth year the child flow refused as too
        old still counts: 18+ means the adult group. The extractor's guess only
        fills a group not chosen yet; switching takes a marker in this message.
        """
        if waiting_for == "audience":
            choice = _audience_choice(user_text)
            if choice:
                return choice
        year = extracted.get("child_birth_year") or rejected.get("child_birth_year", (None, None))[1]
        if waiting_for == "child_name" and extracted.get("child_name"):
            detected = None
        else:
            detected = _detect_audience(user_text, year)
        if detected is None and audience is None:
            detected = extracted.get("audience") or _audience_in_history(history)
        return detected

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
        audience = _stored_audience(draft)
        if audience is None:
            if self._offers_adult_group(bot_name):
                return _audience_prompt(lang)
            audience = "child"
        if audience == "adult":
            return self._evaluate_adult(bot_name, draft, lang)

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

    def _evaluate_adult(self, bot_name: str, draft: dict, lang: str) -> str:
        """The adult group: no age, level or school shift. The schedule comes
        first, the name is asked once a class is picked."""
        slots = trial_logic.get_adult_trial_slots(bot_name)
        if not slots:
            return _loc(lang, "no_groups_call_admin")
        if draft.get("group_id"):
            if _chosen_slot_still_fits(draft, slots):
                return self._adult_next_step(draft, lang)
            result = trial_service.update_intake(bot_name, draft["id"], {**_CLEARED_SLOT, "language": lang})
            if not result["ok"]:
                return result.get("message") or _loc(lang, "no_groups")
            draft = result["data"]["trial"]

        picked, text = _narrow_adult(slots, draft, lang)
        if not picked:
            return text
        result = trial_service.assign_slot(bot_name, draft["id"], picked)
        if not result["ok"]:
            return result.get("message") or _loc(lang, "no_groups")
        return self._adult_next_step(result["data"]["trial"], lang)

    @staticmethod
    def _adult_next_step(draft: dict, lang: str) -> str:
        if not _is_plausible_child_name(draft.get("child_name")):
            return _ask_missing(lang, "child_name", "self")
        return _adult_confirmation(draft, lang)

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
        audience = _stored_audience(draft)
        if audience is None and self._offers_adult_group(bot_name):
            return _audience_prompt(lang)
        if audience == "adult":
            slots = trial_logic.get_adult_trial_slots(bot_name)
            if not slots:
                return None
            if draft.get("group_id") and _chosen_slot_still_fits(draft, slots):
                return self._adult_next_step(draft, lang)
            _, text = _narrow_adult(slots, draft, lang, pick=False, note_unavailable=False)
            return text
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
