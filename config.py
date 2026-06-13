import json as _json
import os
from dotenv import load_dotenv
from chat.system_prompts import sp_1, sp_3, sp_2

load_dotenv()

# OpenAI
OPENAI_API_KEY: str = os.environ["OPENAI_API_KEY"]
MODEL_NAME: str = "gpt-4o-mini"
EMBEDDING_MODEL: str = "text-embedding-3-small"

# WhatsApp Cloud API
WHATSAPP_TOKEN: str = os.environ["WHATSAPP_TOKEN"]
WHATSAPP_PHONE_NUMBER_ID_BOT_1: str = os.environ["WHATSAPP_PHONE_NUMBER_ID_BOT_1"]
WHATSAPP_PHONE_NUMBER_ID_BOT_2: str = os.environ["WHATSAPP_PHONE_NUMBER_ID_BOT_2"]
WHATSAPP_PHONE_NUMBER_ID_BOT_3: str = os.environ["WHATSAPP_PHONE_NUMBER_ID_BOT_3"]
WHATSAPP_VERIFY_TOKEN: str = os.environ["WHATSAPP_VERIFY_TOKEN"]

BOT_CONFIGS = {
    WHATSAPP_PHONE_NUMBER_ID_BOT_1: {
        "name": "dopsy_bot",
        "access_token": WHATSAPP_TOKEN,
        "phone_number_id": WHATSAPP_PHONE_NUMBER_ID_BOT_1,
        "system_prompt": sp_1.SYSTEM_PROMPT,
    },
    WHATSAPP_PHONE_NUMBER_ID_BOT_2: {
        "name": "chatbot_2",
        "access_token": WHATSAPP_TOKEN,
        "phone_number_id": WHATSAPP_PHONE_NUMBER_ID_BOT_2,
        "system_prompt": sp_2.SYSTEM_PROMPT,
    },
    WHATSAPP_PHONE_NUMBER_ID_BOT_3: {
        "name": "dopsy_boxing",
        "access_token": WHATSAPP_TOKEN,
        "phone_number_id": WHATSAPP_PHONE_NUMBER_ID_BOT_3,
        "system_prompt": sp_3.SYSTEM_PROMPT,
    },
}

def get_bot_config(phone_number_id: str) -> dict | None:
    return BOT_CONFIGS.get(phone_number_id)


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------
POSTGRES_DSN: str = os.getenv("POSTGRES_DSN", "")
POSTGRES_MAX_CONN: int = int(os.getenv("POSTGRES_MAX_CONN", "10"))

# ---------------------------------------------------------------------------
# Manager API (Google Apps Script → backend)
# ---------------------------------------------------------------------------
MANAGER_API_KEY: str = os.getenv("MANAGER_API_KEY", "")
MANAGER_RATE_LIMIT: int = int(os.getenv("MANAGER_RATE_LIMIT", "60"))  # requests/min per IP

# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------
GOOGLE_CREDENTIALS_PATH: str = os.getenv("GOOGLE_CREDENTIALS_PATH", "./secrets/google_credentials.json")
GOOGLE_SPREADSHEET_ID: str = os.getenv("GOOGLE_SPREADSHEET_ID", "")
GOOGLE_WORKSHEET_NAME: str = os.getenv("GOOGLE_WORKSHEET_NAME", "Bookings")

# ---------------------------------------------------------------------------
# Booking (Bot 1 — Dopshy field rental only)
# ---------------------------------------------------------------------------
BOOKING_OPEN_TIME: str = os.getenv("BOOKING_OPEN_TIME", "09:00")
BOOKING_CLOSE_TIME: str = os.getenv("BOOKING_CLOSE_TIME", "23:00")
BOOKING_SLOT_DURATION: int = int(os.getenv("BOOKING_SLOT_DURATION", "60"))  # minutes
BOOKING_FIELDS: list = _json.loads(
    os.getenv("BOOKING_FIELDS", '[{"id":1,"format":"5x5"},'
                                '{"id":2,"format":"6x6"},'
                                '{"id":3,"format":"5x5"}]')
)
BOOKING_TIMEZONE: str = os.getenv("BOOKING_TIMEZONE", "Asia/Almaty")
BOOKING_SESSION_TTL: int = int(os.getenv("BOOKING_SESSION_TTL", "1800"))  # seconds
PAYMENT_TTL_SECONDS: int = int(os.getenv("PAYMENT_TTL_SECONDS", "900"))   # 1 hour
KASPI_PAYMENT_URL: str = os.getenv("KASPI_PAYMENT_URL", "https://pay.kaspi.kz/pay/z7xcvrgq")

# Payment receipt validation
PAYMENT_MIN_FRACTION: float = float(os.getenv("PAYMENT_MIN_FRACTION", "0.5"))           # min share of full price
PAYMENT_RECEIPT_MAX_AGE_HOURS: int = int(os.getenv("PAYMENT_RECEIPT_MAX_AGE_HOURS", "24"))


def get_whatsapp_api_url(phone_number_id : str) -> str:
    return f"https://graph.facebook.com/v22.0/{phone_number_id}/messages"

# ChromaDB
CHROMA_DB_PATH: str = os.getenv("CHROMA_DB_PATH", "./chroma_db")
CHROMA_COLLECTION_NAME: str = "football_rental_docs"

# Documents
DOCUMENTS_PATH: str = os.getenv("DOCUMENTS_PATH", "./documents")

# RAG
TOP_K_RESULTS: int = 3
CHUNK_SIZE: int = 500
CHUNK_OVERLAP: int = 50

# Conversation
MAX_HISTORY_MESSAGES: int = 20  # total messages kept per chat (user+assistant)
CONVERSATION_DB_PATH: str = os.getenv("CONVERSATION_DB_PATH", "./data/conversations.db")
