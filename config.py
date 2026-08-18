import json as _json
import os
from datetime import datetime
from dotenv import load_dotenv
from chat.system_prompts import sp_1, sp_3, sp_2

load_dotenv()

# OpenAI
OPENAI_API_KEY: str = os.environ["OPENAI_API_KEY"]
MODEL_NAME: str = "gpt-5.2"
EXTRACTOR_MODEL: str = "gpt-4.1"
INTENT_MODEL: str = "gpt-4.1"
EMBEDDING_MODEL: str = "text-embedding-3-small"

# WhatsApp Cloud API
WHATSAPP_TOKEN: str = os.environ["WHATSAPP_TOKEN"]
WHATSAPP_SECOND_TOKEN: str = os.environ["WHATSAPP_SECOND_TOKEN"]

WHATSAPP_PHONE_NUMBER_ID_BOT_1: str = os.environ["WHATSAPP_PHONE_NUMBER_ID_BOT_1"]
WHATSAPP_PHONE_NUMBER_ID_BOT_2: str = os.environ["WHATSAPP_PHONE_NUMBER_ID_BOT_2"]
WHATSAPP_PHONE_NUMBER_ID_BOT_3: str = os.environ["WHATSAPP_PHONE_NUMBER_ID_BOT_3"]
WHATSAPP_VERIFY_TOKEN: str = os.environ["WHATSAPP_VERIFY_TOKEN"]

YCLOUD_API_KEY: str = os.environ["YCLOUD_API_KEY_1"]
YCLOUD_API_KEY_2: str = os.environ["YCLOUD_API_KEY_2"]

YCLOUD_FROM_BOT_1: str = os.environ["YCLOUD_FROM_BOT_1"]
YCLOUD_FROM_BOT_2: str = os.environ["YCLOUD_FROM_BOT_2"]
YCLOUD_FROM_BOT_3: str = os.environ["YCLOUD_FROM_BOT_3"]

MESSAGE_BATCH_WINDOW_SECONDS: float = float(
    os.getenv("MESSAGE_BATCH_WINDOW_SECONDS", "4")
)

BOT_CONFIGS = {
    WHATSAPP_PHONE_NUMBER_ID_BOT_1: {
        "name": "dopsy_bot",
        "access_token": WHATSAPP_TOKEN,
        "phone_number_id": WHATSAPP_PHONE_NUMBER_ID_BOT_1,
        "ycloud_api_key": YCLOUD_API_KEY,
        "ycloud_from": YCLOUD_FROM_BOT_1,
        "system_prompt": sp_1.SYSTEM_PROMPT,
    },
    WHATSAPP_PHONE_NUMBER_ID_BOT_2: {
        "name": "dopsy_fs_school",
        "access_token": WHATSAPP_TOKEN,
        "phone_number_id": WHATSAPP_PHONE_NUMBER_ID_BOT_2,
        "ycloud_api_key": YCLOUD_API_KEY_2,
        "ycloud_from": YCLOUD_FROM_BOT_2,
        "system_prompt": sp_2.SYSTEM_PROMPT,
    },
    WHATSAPP_PHONE_NUMBER_ID_BOT_3: {
        "name": "dopsy_boxing",
        "access_token": WHATSAPP_SECOND_TOKEN,
        "phone_number_id": WHATSAPP_PHONE_NUMBER_ID_BOT_3,
        "ycloud_api_key": YCLOUD_API_KEY,
        "ycloud_from": YCLOUD_FROM_BOT_3,
        "system_prompt": sp_3.SYSTEM_PROMPT,
    },
}

def get_bot_config(phone_number_id: str) -> dict | None:
    return BOT_CONFIGS.get(phone_number_id)


def _normalize_phone(value: str | None) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def get_phone_number_id_for_ycloud_from(ycloud_from: str | None) -> str | None:
    """Resolve YCloud's inbound `to` number to this app's bot config key."""
    normalized = _normalize_phone(ycloud_from)
    if not normalized:
        return None

    for phone_number_id, bot_config in BOT_CONFIGS.items():
        if _normalize_phone(bot_config.get("ycloud_from")) == normalized:
            return phone_number_id
    return None


def resolve_inbound_phone_number_id(provider: str, business_phone_number_id: str | None,
                                    business_phone: str | None = None) -> str | None:
    if provider == "ycloud":
        return get_phone_number_id_for_ycloud_from(business_phone)
    return business_phone_number_id


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------
POSTGRES_DSN: str = os.getenv("POSTGRES_DSN", "")
POSTGRES_MAX_CONN: int = int(os.getenv("POSTGRES_MAX_CONN", "10"))

# ---------------------------------------------------------------------------
# Redis — shared message-batch buffer (survives across gunicorn workers).
# ---------------------------------------------------------------------------
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# ---------------------------------------------------------------------------
# Manager API (Google Apps Script → backend)
# ---------------------------------------------------------------------------
X_SERVICE_TOKEN: str = os.getenv("X_SERVICE_TOKEN", "")
MANAGER_RATE_LIMIT: int = int(os.getenv("MANAGER_RATE_LIMIT", "60"))  # requests/min per IP
PAGE_SIZE: int = int(os.getenv("PAGE_SIZE", "20"))  # default rows per page for paginated endpoints

# Origins allowed to call the browser-facing manager API (CORS). Comma-separated
# env list, e.g. "https://a.example.com,https://b.example.com". "*" (the default)
# allows any origin. flask-cors needs a list for multiple origins, so split here.
_cors_origins_raw = os.getenv("CORS_ALLOWED_ORIGINS", "*").strip()
CORS_ALLOWED_ORIGINS = (
    "*" if _cors_origins_raw in ("", "*")
    else [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
)

# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------
GOOGLE_CREDENTIALS_PATH: str = os.getenv("GOOGLE_CREDENTIALS_PATH", "./secrets/google_credentials.json")
GOOGLE_SPREADSHEET_ID: str = os.getenv("GOOGLE_SPREADSHEET_ID", "")
GOOGLE_WORKSHEET_NAME: str = os.getenv("GOOGLE_WORKSHEET_NAME", "Bookings")

# ---------------------------------------------------------------------------
# Booking (Bot 1 — Dopshy field rental only)
# ---------------------------------------------------------------------------
BOOKING_OPEN_TIME: str = os.getenv("BOOKING_OPEN_TIME", "00:00")
BOOKING_CLOSE_TIME: str = os.getenv("BOOKING_CLOSE_TIME", "23:59")
BOOKING_SLOT_DURATION: int = int(os.getenv("BOOKING_SLOT_DURATION", "60"))  # minutes
BOOKING_FIELDS: list = _json.loads(
    os.getenv("BOOKING_FIELDS", '[{"id":1,"format":"6x6"},'
                                '{"id":2,"format":"5x5"},'
                                '{"id":3,"format":"5x5"}]')
)
BOOKING_TIMEZONE: str = os.getenv("BOOKING_TIMEZONE", "Asia/Almaty")
BOOKING_SESSION_TTL: int = int(os.getenv("BOOKING_SESSION_TTL", "1200"))  # seconds
PAYMENT_TTL_SECONDS: int = int(os.getenv("PAYMENT_TTL_SECONDS", "1200"))  # 20 minutes
KASPI_PAYMENT_URL: str = os.getenv("KASPI_PAYMENT_URL", "https://pay.kaspi.kz/pay/z7xcvrgq")

# Payment receipt validation
PAYMENT_MIN_FRACTION: float = float(os.getenv("PAYMENT_MIN_FRACTION", "0.5"))           # min share of full price
PAYMENT_RECEIPT_MAX_AGE_HOURS: int = int(os.getenv("PAYMENT_RECEIPT_MAX_AGE_HOURS", "24"))
PAYMENT_MIN: int = 10000

# ---------------------------------------------------------------------------
# ApiPay.kz — online avans via Kaspi Pay (https://apipay.kz)
# ---------------------------------------------------------------------------
# Server-side only: the API key must never reach a browser or the Apps Script.
APIPAY_API_KEY: str = os.getenv("APIPAY_API_KEY", "")
APIPAY_WEBHOOK_SECRET: str = os.getenv("APIPAY_WEBHOOK_SECRET", "")
APIPAY_BASE_URL: str = os.getenv("APIPAY_BASE_URL", "https://api.apipay.kz/api/v1")
APIPAY_TIMEOUT: float = float(os.getenv("APIPAY_TIMEOUT", "10"))
# Avans charged per non-repeating booking in a bookings/batch request. Repeating
# slots are never charged an avans (one invoice per batch = this x slot count).
APIPAY_AVANS_PER_BOOKING: int = int(os.getenv("APIPAY_AVANS_PER_BOOKING", "10000"))

# Ask `POST /clients/check` whether the number is registered in Kaspi before
# any invoice is raised for it. On by default: an invoice to a number Kaspi does
# not know fails only later, via webhook, so neither the client nor the manager
# would learn about it in the request that asked — and it still costs one of the
# account's daily invoices. Set to 0 only if the endpoint itself misbehaves.
APIPAY_CHECK_CLIENT: bool = os.getenv("APIPAY_CHECK_CLIENT", "1").strip().lower() \
    not in ("0", "false", "no", "off")

# The whole integration is inert until both secrets are present, so a machine
# without them keeps the old manual-receipt flow instead of failing batches.
APIPAY_ENABLED: bool = bool(APIPAY_API_KEY and APIPAY_WEBHOOK_SECRET)


def get_whatsapp_api_url(phone_number_id : str) -> str:
    return f"https://graph.facebook.com/v22.0/{phone_number_id}/messages"

def get_ycloud_api_url() -> str:
    return "https://api.ycloud.com/v2/whatsapp/messages"

def get_ycloud_mark_as_read_url(message_id: str) -> str:
    return f"https://api.ycloud.com/v2/whatsapp/inboundMessages/{message_id}/markAsRead"

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

MAX_PLAYERS: int = 100

# ---------------------------------------------------------------------------
# Holidays (dates that use weekend_holiday pricing)
# ---------------------------------------------------------------------------
_raw_holidays = os.getenv("HOLIDAYS", "")
HOLIDAYS: set = {
    datetime.date(datetime.strptime(d.strip(), "%Y-%m-%d"))
    for d in _raw_holidays.split(",") if d.strip()
}
