"""OpenAI GPT-4o-mini integration."""

from openai import OpenAI

import config

_client = OpenAI(api_key=config.OPENAI_API_KEY)

SYSTEM_PROMPT = """Ты — умный ассистент компании по аренде футбольных полей «Допши».
Ты общаешься с клиентами в WhatsApp на русском или казахском языке — всегда отвечай на том языке, на котором написал пользователь.

Твои задачи:
- Отвечать на вопросы об аренде полей: расписание, цены, правила, бронирование
- Помогать группам игроков организовать игру
- Давать чёткие, дружелюбные и короткие ответы
- При необходимости уточнять детали (дату, время, количество игроков)

Правила поведения:
- Отвечай только по теме аренды полей и организации игр
- Если вопрос вне твоей компетенции — вежливо перенаправь к администратору
- Не выдумывай информацию, которой нет в базе знаний — скажи, что уточнишь у команды
- Будь кратким: 2–4 предложения на ответ, если не требуется подробный список

---

Ты — ассистент «Допши» жалдау компаниясының ақылды көмекшісісің.
Клиенттермен WhatsApp арқылы орысша немесе қазақша сөйлесесің — пайдаланушы қай тілде жазса, сол тілде жауап бер.

Міндеттерің:
- Алаңды жалдау туралы сұрақтарға жауап беру: кесте, баға, ережелер, брондау
- Ойыншылар тобына ойын ұйымдастыруға көмектесу
- Нақты, мейірімді және қысқа жауаптар беру
"""


def get_ai_response(
    chat_id: str,
    user_message: str,
    history: list[dict],
    context: str,
) -> str:
    """
    Build the full prompt with RAG context and conversation history,
    then call GPT-4o-mini and return the assistant reply.
    """
    system_content = SYSTEM_PROMPT
    if context:
        system_content += f"\n\n--- База знаний / Білім базасы ---\n{context}\n---"

    messages = [{"role": "system", "content": system_content}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    response = _client.chat.completions.create(
        model=config.MODEL_NAME,
        messages=messages,
        temperature=0.6,
        max_tokens=512,
    )
    return response.choices[0].message.content.strip()
