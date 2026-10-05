"""OpenAI GPT-4.1 integration."""
import config
import json
import logging


# Shared instrumented client: the agent-test console traces model, system
# prompt, tokens and latency for every call made through it.
from chat.openai_client import client as _client
from chat.conversation import Message
from chat.system_prompts.sp_1 import INTENT_PROMPT
from chat.tools.arena_tools import EDIT_BOOKING_TOOL, START_BOOKING_TOOL, SELECT_INTENT_LLM
from chat.tools.academy_tools import (
    START_TRIAL_TOOL,
    EDIT_TRIAL_TOOL,
    CANCEL_TRIAL_TOOL,
    SELECT_TRIAL_INTENT_LLM,
)

logger = logging.getLogger(__name__)


def get_ai_response(
        phone_number_id: str,
        chat_id: str,
        user_message: str,
        history: list[Message],
        context: str,
) -> tuple[str, dict | None]:
    """
    Build the full prompt with RAG context and conversation history,
    then call GPT-4o-mini.

    Returns:
        (reply_text, tool_call)
        tool_call is None if the LLM produced a plain reply, otherwise a dict
        of shape {"name": "start_booking" | "edit_booking", "args": {...}}.
        The handler dispatches on `name` to launch the corresponding flow.
    """
    is_dopsy = config.BOT_CONFIGS[phone_number_id]["name"] == "dopsy_bot"
    system_content = config.BOT_CONFIGS[phone_number_id]["system_prompt"]

    # Inject factual field list for Bot 1 so the LLM never hallucinates field formats
    if is_dopsy and config.BOOKING_FIELDS:
        formats = sorted({f["format"] for f in config.BOOKING_FIELDS})
        fields_lines = "\n".join(f"  - {fmt}" for fmt in formats)
        system_content += f"\n\n--- Доступные размеры полей ---\n{fields_lines}\n---"

    if context:
        system_content += f"\n\n--- База знаний / Білім базасы ---\n{context}\n---"

    messages = [{"role": "system", "content": system_content}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    kwargs = dict(
        model=config.MODEL_NAME,
        messages=messages,
        temperature=0.2 if is_dopsy else 0.6,
        max_completion_tokens=512,
    )
    if not is_dopsy:
        # start_trial retired: trial_new / trial_continue reach LlmTrialFlowHandler
        # through the intent router, before this call happens.
        kwargs["tools"] = [EDIT_TRIAL_TOOL, CANCEL_TRIAL_TOOL]
        kwargs["tool_choice"] = "auto"

    response = _client.chat.completions.create(**kwargs)
    msg = response.choices[0].message
    preamble = (msg.content or "").strip()

    if msg.tool_calls:
        for tc in msg.tool_calls:
            name = tc.function.name
            if name in ("edit_trial", "cancel_trial"):
                args: dict = {}
                if name in ("edit_trial"):
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        logger.warning(
                            "[LLM] edit_booking returned unparseable arguments: %r",
                            tc.function.arguments,
                        )
                        args = {}
                return preamble, {"name": name, "args": args}

    return preamble, None


def get_booking_reply(
        user_text: str,
        booking_context: str,
        system_hint: str = "",
) -> str:
    """
    Generate a natural-language reply for booking-related queries.
    Responds in the same language the user wrote in (Russian or Kazakh).
    """
    system_content = (
        "Ты — ассистент по бронированию футбольных полей «Допши». "
        "Всегда отвечай на том языке, на котором написал пользователь (русский или казахский). "
        "Будь кратким и дружелюбным. Не придумывай информацию. "
        "ВСЕГДА обращайся к клиенту только на «вы», никогда не переходи на «ты», "
        "даже если клиент пишет неформально. "
        "Клиентке ӘРҚАШАН тек «сіз» деп қарата сөйле, ешқашан «сен» деп ауыспа, "
        "клиент бейресми жазса да."
    )
    if system_hint:
        system_content += f"\n\nИнструкция: {system_hint}"
    if booking_context:
        system_content += f"\n\n--- Данные о бронировании ---\n{booking_context}\n---"

    response = _client.chat.completions.create(
        model=config.MODEL_NAME,
        messages=[
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_text},
        ],
        temperature=0.5,
        max_completion_tokens=400,
    )
    return response.choices[0].message.content.strip()


def get_trial_reply(
        user_text: str,
        context: str = "",
        system_hint: str = "",
) -> str:
    """Generate a short natural-language reply for an academy trial-signup
    conversation that has drifted off the current intake question.

    Used when a message during a gated trial-signup flow is neither a data
    value, a yes/no, nor a recognized interrupt (greeting/identity/ack) — an
    objection, a side question, or anything else. The caller appends its own
    reminder of what's still needed, so this only needs to answer briefly.
    """
    system_content = (
        "Ты — ассистент детской спортивной академии. "
        "Всегда отвечай на том языке, на котором написал пользователь (русский или казахский). "
        "Будь кратким (1-2 предложения) и дружелюбным. Не придумывай факты, которых нет в базе знаний. "
        "ВСЕГДА обращайся на «вы», никогда на «ты», даже если пишут неформально. "
        "ӘРҚАШАН «сіз» деп қарата сөйле, ешқашан «сен» деп ауыспа."
    )
    if system_hint:
        system_content += f"\n\nИнструкция: {system_hint}"
    if context:
        system_content += f"\n\n--- База знаний ---\n{context}\n---"

    response = _client.chat.completions.create(
        model=config.MODEL_NAME,
        messages=[
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_text},
        ],
        temperature=0.4,
        max_completion_tokens=250,
    )
    return response.choices[0].message.content.strip()


def route_incoming_message(history: list, user_message: str,
                           prompt: str | None = None,
                           tool: dict | None = None) -> tuple[str, str]:
    """Classify the intent and language of the latest user message.

    Defaults to the arena classifier. The academy bots pass sp_academy's
    TRIAL_INTENT_PROMPT with SELECT_TRIAL_INTENT_LLM — a different intent enum
    over the same call shape. "other" remains the failure value in both enums.

    Returns:
        (intent, lang) — intent is one of the strict enum values ("other" on failure),
        lang is "ru" or "kk".
    """
    prompt = prompt or INTENT_PROMPT
    tool = tool or SELECT_INTENT_LLM

    messages = [{"role": "system", "content": prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    try:
        response = _client.chat.completions.create(
            model=config.INTENT_MODEL,
            temperature=0,
            messages=messages,
            tools=[tool],
            tool_choice={"type": "function",
                         "function": {"name": tool["function"]["name"]}}
        )

        tool_calls = response.choices[0].message.tool_calls
        if not tool_calls:
            return "other", "ru"

        raw_args = tool_calls[0].function.arguments
        if not raw_args:
            return "other", "ru"

        data = json.loads(raw_args)
        return data.get("type", "other"), data.get("lang", "ru")

    except Exception as err:
        logging.error(f"route_incoming_message failed: {err}")
        return "other", "ru"


def route_trial_message(
    history: list,
    user_message: str,
    pending: str | None = None,
    fallback: str | None = "other",
) -> tuple[str | None, str]:
    """Classify the latest academy/QA bot message into a strict trial intent.

    `pending` is the question the bot is waiting on during a signup; it lets
    the router tell an answer from a side question. `fallback` is returned
    when classification fails — None lets a mid-signup caller keep the
    message in the flow instead of treating it as "other".
    """
    system_content = (
        "You route WhatsApp messages for academy trial signup bots. "
        "Greetings, thanks, and small talk such as hello/hi/привет/сәлем/че там/как дела are always other. "
        "Questions about the bot itself such as 'что это за бот', 'кто ты', 'что ты умеешь' are always other. "
        "Return one intent only. Use trial_new when the user wants to sign up, "
        "try a child/group trial class, join child/group training, or asks to come to a trial. "
        "Do not use trial_new or trial_continue for adult, individual, personal, one-on-one, "
        "girls/women self-training, or consultation-only threads; use question_personal_training "
        "or question_adult_training instead, including follow-up replies like 'да, для себя с нуля'. "
        "Use question_schedule/price/location/age/trial_rules for factual questions. "
        "Use question_personal_training for personal/individual/one-on-one training questions. "
        "Use question_adult_training for adult self-training questions. "
        "Use question_child_training for factual child group questions that do not yet ask to book. "
        "Use question_payment for payment method/payment day questions, question_discounts for discounts/promotions, "
        "and question_contacts for phone/address/contact questions. "
        "Use question_invoice when the user asks to be sent an invoice, bill or payment "
        "details — e.g. 'шот жіберіңіз', 'маған шот жіберіңіз', 'төлемге шот керек', "
        "'скиньте счёт', 'выставьте счёт', 'реквизиты для оплаты'. This is never "
        "trial_status or trial_cancel. "
        "Use question_voucher for anything about a voucher (ваучер, Qosymsha, ED24, "
        "госзаказ, перевод/ауысу по ваучеру from another section, исторический ваучер, "
        "documents or steps for voucher enrolment) — e.g. 'ваучермен ауысуға бола ма', "
        "'ваучерді қалай ауыстырамын', 'как перевести ваучер к вам', 'у нас есть ваучер'. "
        "This is never question_payment or trial_new, even if they also want to join. "
        "Use trial_continue only when the user is clearly providing missing signup details "
        "for an already-started child/group trial signup, such as child name, birth year, "
        "experience, school shift, or preferred date/time. "
        "Use trial_status when the user asks about a signup they already have — e.g. "
        "'мои занятия', 'какие у меня есть пробные', 'моя запись', 'когда у меня занятие', "
        "'жазылымым', 'менің сабағым' — this is a lookup, not a new signup, even if the "
        "wording overlaps with trial_new. "
        "Use trial_cancel when the user wants to cancel or withdraw an existing signup — "
        "e.g. 'хочу отменить', 'отмените запись', 'больше не хочу заниматься', 'бас тартамын'. "
        "Use trial_edit when the user wants to change a detail of an existing signup — "
        "e.g. 'перенесите на другое время', 'поменяйте дату', 'измените имя ребенка'. "
        "Use human_help when they ask for an administrator or human manager. "
        "Use unclear when you cannot tell what the user means: garbled or random text, "
        "a fragment with no clear question or request, or a message whose meaning is "
        "ambiguous even with the conversation history. Do not use unclear for greetings, "
        "thanks, or a short answer to the bot's own question. "
        "Detect Russian as ru and Kazakh as kk."
    )
    if pending:
        system_content += (
            " The client is in the middle of a trial signup. The bot's pending question was:\n"
            f"{pending}\n"
            "Use trial_continue only if the message answers that question or changes signup data. "
            "A question about the schedule, trainers, prices, etc. is NOT trial_continue, even if it "
            "mentions a day or a trainer from the options."
        )
    messages = [{"role": "system", "content": system_content}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    try:
        response = _client.chat.completions.create(
            model=config.INTENT_MODEL,
            temperature=0,
            messages=messages,
            tools=[SELECT_TRIAL_INTENT_LLM],
            tool_choice={"type": "function", "function": {"name": "route_trial_message"}},
        )

        tool_calls = response.choices[0].message.tool_calls
        if not tool_calls:
            return fallback, "ru"

        raw_args = tool_calls[0].function.arguments
        if not raw_args:
            return fallback, "ru"

        data = json.loads(raw_args)
        return data.get("type", fallback), data.get("lang", "ru")

    except Exception as err:
        logging.error(f"route_trial_message failed: {err}")
        return fallback, "ru"
