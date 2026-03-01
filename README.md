# Dopshy Bot — WhatsApp AI Assistant for Football Field Rental

A production-ready WhatsApp chatbot for **Допши (Dopshy)** — a football field rental company in Almaty, Kazakhstan. The bot answers client questions in **Russian and Kazakh** using a RAG (Retrieval-Augmented Generation) pipeline powered by OpenAI and ChromaDB, with full conversation history persistence.

---

## Features

- **Bilingual** — auto-detects and responds in Russian or Kazakh
- **RAG system** — answers are grounded in your own knowledge base documents (no hallucinations)
- **Persistent conversation history** — SQLite-backed, survives restarts; bot remembers what was already discussed
- **Group chat support** — works in WhatsApp group conversations
- **Blue ticks** — marks messages as read instantly
- **Hot-reload knowledge base** — update `.md` files and re-index without restarting
- **Docker-first** — single `docker compose up` to run everything

---

## Architecture

```
WhatsApp User
     │
     ▼
Meta Cloud API  ──POST──►  Flask Webhook (/webhook)
                                  │
                    ┌─────────────┘
                    │
                    ▼
          message_handler.py
                    │
          ┌─────────┼──────────┐
          │         │          │
          ▼         ▼          ▼
    RAG Retriever  History   WhatsApp
    (ChromaDB)    (SQLite)    Client
          │         │
          └────┬────┘
               │
               ▼
         GPT-4o-mini
         (OpenAI API)
               │
               ▼
        Reply sent back
        via Cloud API
```

### Request pipeline (per message)

1. Webhook receives message → immediately returns `200 OK` → processes in background thread
2. Incoming message is marked as read (blue ticks)
3. Top-3 relevant chunks are retrieved from ChromaDB using semantic search
4. Last 20 messages of conversation history are loaded from SQLite
5. System prompt + RAG context + history + user message → GPT-4o-mini
6. Response is saved to SQLite and sent back via WhatsApp Cloud API

---

## Project Structure

```
bot/
├── app.py                      # Flask webhook server
├── config.py                   # Centralised config (loaded from .env)
├── pyproject.toml              # Poetry dependency management
├── Dockerfile
├── docker-compose.yml
├── .env.example
│
├── rag/
│   ├── vector_store.py         # ChromaDB — document ingestion & chunking
│   └── retriever.py            # Semantic search with in-process cache
│
├── chat/
│   ├── llm.py                  # OpenAI GPT-4o-mini integration + system prompt
│   └── conversation.py         # Persistent history (SQLite write-through cache)
│
├── handlers/
│   ├── message_handler.py      # Orchestrates the full pipeline
│   └── whatsapp_client.py      # Meta WhatsApp Cloud API HTTP client
│
├── scripts/
│   └── ingest.py               # CLI script to index documents into ChromaDB
│
└── documents/                  # Knowledge base (edit these to customise the bot)
    ├── services_ru.md
    ├── services_kz.md
    ├── pricing_ru.md
    ├── rules_ru.md
    ├── faq_ru.md
    └── faq_kz.md
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.12+ | Or use Docker (no local Python needed) |
| [Poetry](https://python-poetry.org/docs/#installation) | For local development |
| OpenAI API key | [platform.openai.com](https://platform.openai.com) |
| Meta Developer account | [developers.facebook.com](https://developers.facebook.com) |
| WhatsApp Business phone number | Verified in Meta Business Manager |
| [ngrok](https://ngrok.com) *(dev only)* | To expose localhost to Meta |

---

## Quick Start — Docker (recommended)

### 1. Clone and configure

```bash
git clone https://github.com/your-username/dopshy-bot.git
cd dopshy-bot
cp .env.example .env
```

Open `.env` and fill in your credentials (see [Environment Variables](#environment-variables)).

### 2. Index the knowledge base

```bash
docker compose --profile ingest run --rm ingest
```

This reads all `.md` files from `documents/`, splits them into chunks, embeds them with OpenAI, and stores them in ChromaDB. Run this again whenever you update the documents.

### 3. Start the bot

```bash
docker compose up -d
```

The bot is now running on port `5000`. Check logs with:

```bash
docker compose logs -f bot
```

### 4. Expose to Meta (local dev)

```bash
ngrok http 5000
```

Copy the `https://xxxx.ngrok.io` URL, then in Meta Developer Console:

- Go to **WhatsApp → Configuration → Webhook**
- Set **Callback URL**: `https://xxxx.ngrok.io/webhook`
- Set **Verify token**: the value of `WHATSAPP_VERIFY_TOKEN` in your `.env`
- Subscribe to the **messages** field

---

## Quick Start — Local (without Docker)

```bash
# Install dependencies
poetry install

# Index documents
poetry run python scripts/ingest.py

# Start server
poetry run python app.py
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in all values.

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | Yes | OpenAI API key (`sk-...`) |
| `WHATSAPP_TOKEN` | Yes | Permanent access token from Meta Developer Console |
| `WHATSAPP_PHONE_NUMBER_ID` | Yes | Phone Number ID from Meta (not the phone number itself) |
| `WHATSAPP_VERIFY_TOKEN` | Yes | Any random string you choose — used to verify the webhook with Meta |
| `FLASK_SECRET_KEY` | Yes | Random string for Flask session security |
| `CHROMA_DB_PATH` | No | Path to ChromaDB storage (default: `./chroma_db`) |
| `DOCUMENTS_PATH` | No | Path to knowledge base documents (default: `./documents`) |
| `CONVERSATION_DB_PATH` | No | Path to SQLite conversation history DB (default: `./data/conversations.db`) |
| `PORT` | No | Port to run Flask on (default: `5000`) |

### How to get WhatsApp credentials

1. Go to [developers.facebook.com](https://developers.facebook.com) → **My Apps → Create App**
2. Add **WhatsApp** product
3. Under **WhatsApp → API Setup**:
   - Copy **Phone Number ID** → `WHATSAPP_PHONE_NUMBER_ID`
   - Generate a **Permanent Token** via System User in Business Manager → `WHATSAPP_TOKEN`
4. Set any string as `WHATSAPP_VERIFY_TOKEN` (e.g. `my-secret-token-123`)

---

## Knowledge Base

The bot's knowledge lives in the `documents/` folder. Each `.md` or `.txt` file is automatically chunked and indexed into ChromaDB.

### Editing the knowledge base

1. Edit or add files in `documents/`
2. Re-index:

```bash
# Docker
docker compose --profile ingest run --rm ingest

# Local
poetry run python scripts/ingest.py

# Or via HTTP (no restart needed)
curl -X POST http://localhost:5000/admin/ingest \
     -H "X-Admin-Token: your_WHATSAPP_VERIFY_TOKEN"
```

### Current documents

| File | Language | Contents |
|---|---|---|
| `services_ru.md` | Russian | Field types, booking info, opening hours |
| `services_kz.md` | Kazakh | Field types, booking info, opening hours |
| `pricing_ru.md` | Russian | Hourly rates, discounts, cancellation policy |
| `rules_ru.md` | Russian | Field rules, equipment, locker rooms |
| `faq_ru.md` | Russian | Frequently asked questions |
| `faq_kz.md` | Kazakh | Frequently asked questions |

Add as many documents as you need in either language. The RAG system handles them all automatically.

---

## Conversation History

Each WhatsApp user (identified by phone number) has their own persistent conversation thread stored in SQLite at `data/conversations.db`.

- The last **20 messages** (10 full turns) are included in every request to GPT-4o-mini
- History survives server/container restarts
- **Reset commands** — a user can clear their own history by sending any of:

| Command | Language |
|---|---|
| `/reset` | English |
| `/сброс` | Russian |
| `/тазалау` | Kazakh |

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/webhook` | Meta webhook verification handshake |
| `POST` | `/webhook` | Receive incoming WhatsApp messages |
| `POST` | `/admin/ingest` | Re-index knowledge base documents |
| `GET` | `/health` | Health check — returns `{"status": "healthy"}` |

The `/admin/ingest` endpoint requires the `X-Admin-Token` header set to your `WHATSAPP_VERIFY_TOKEN`.

---

## Updating Documents Without Downtime

Because `/admin/ingest` runs in the same process and invalidates the in-memory retriever cache, you can update the knowledge base **without restarting the bot**:

```bash
# 1. Edit files in documents/
# 2. Trigger re-index
curl -X POST https://your-domain.com/admin/ingest \
     -H "X-Admin-Token: your_WHATSAPP_VERIFY_TOKEN"
```

---

## Tech Stack

| Component | Technology |
|---|---|
| Language | Python 3.12 |
| Web framework | Flask 3.1 |
| LLM | OpenAI GPT-4o-mini |
| Embeddings | OpenAI text-embedding-3-small |
| Vector database | ChromaDB (local, persistent) |
| RAG framework | LangChain |
| Conversation storage | SQLite (built-in Python) |
| WhatsApp integration | Meta WhatsApp Cloud API |
| Dependency management | Poetry |
| Containerisation | Docker + Docker Compose |

---

## License

MIT
