"""OpenAI GPT-4o-mini integration."""
from openai import OpenAI
import config

_client = OpenAI(api_key=config.OPENAI_API_KEY)

def get_ai_response(
    phone_number_id: str,
    chat_id: str,
    user_message: str,
    history: list[dict],
    context: str,
) -> str:
    """
    Build the full prompt with RAG context and conversation history,
    then call GPT-4o-mini and return the assistant reply.
    """
    system_content = config.BOT_CONFIGS[phone_number_id]["system_prompt"]
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
