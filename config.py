import os
from dotenv import load_dotenv

load_dotenv()

# OpenAI
OPENAI_API_KEY: str = os.environ["OPENAI_API_KEY"]
MODEL_NAME: str = "gpt-4o-mini"
EMBEDDING_MODEL: str = "text-embedding-3-small"

# WhatsApp Cloud API
WHATSAPP_TOKEN: str = os.environ["WHATSAPP_TOKEN"]
WHATSAPP_PHONE_NUMBER_ID: str = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
WHATSAPP_VERIFY_TOKEN: str = os.environ["WHATSAPP_VERIFY_TOKEN"]
WHATSAPP_API_URL: str = (
    f"https://graph.facebook.com/v21.0/{os.environ.get('WHATSAPP_PHONE_NUMBER_ID', '')}/messages"
)

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
