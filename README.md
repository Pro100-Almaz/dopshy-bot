# Dopshy Bot — Multi-Bot WhatsApp Assistant + Booking Engine

Production-ready WhatsApp service for three businesses out of a single Flask process:

- **Bot 1 — Допши (Almaty)** — football-field rental with a full booking state machine, Kaspi payment-receipt validation, Google Sheets sync, and a manager UI in Google Sheets.
- **Bot 2 — FS DOPȘÝ (Astana)** — kids' football school. Knowledge baked into the system prompt.
- **Bot 3 — Boxy Academy (Astana)** — boxing academy. Knowledge baked into the system prompt.

All three share one Flask app, one WhatsApp token, and route by `phone_number_id`. Conversation history is keyed `{phone_number_id}:{sender}` so each bot keeps its own thread per user.

Languages: **Russian and Kazakh** — auto-detected per turn. Bot 1's booking flow is sticky-localized: language is detected at flow start and every subsequent step stays in that language.

---

## Features

- **Multi-bot** — three WhatsApp numbers served from one process, each with its own system prompt.
- **RAG knowledge base** (Bot 1) — answers grounded in `documents/*.md`, indexed into ChromaDB.
- **Deterministic booking state machine** — Postgres-backed, six steps (date → time → field → players → name → confirm).
- **Slot-locking via `EXCLUDE` constraint** — DB-enforced no-overlap, tested under 50-thread race.
- **Payment receipt validation** — parses Kaspi & Halyk PDF receipts (`pypdf`), checks recipient/amount/date.
- **Google Sheets sync** — flat "Bookings" worksheet mirrors Postgres; managers act through a container-bound Apps Script UI that calls `manager_api`.
- **Persistent conversation history** — SQLite (WAL) with write-through cache.
- **Hot-reload knowledge base** — re-index without restart via `/admin/ingest`.
- **Docker-first** — `docker compose up -d` brings up bot + Postgres.

---

## Stack

| Component | Tech |
|---|---|
| Language | Python 3.12 |
| Web framework | Flask 3 (Gunicorn in container) |
| LLM | OpenAI GPT-4o-mini |
| Embeddings | OpenAI text-embedding-3-small |
| Vector DB | ChromaDB (persistent) |
| Booking source-of-truth | PostgreSQL 16 (raw psycopg2, no ORM) |
| Conversation history | SQLite (WAL) |
| Manager UI | Google Apps Script (container-bound) |
| Receipt parsing | pypdf (Kaspi/Halyk fiscal + P2P) |
| Scheduler | APScheduler (sweeper + sheet refresh) |
| Deps / packaging | Poetry |
| Container | Docker + docker-compose |

---

## Request flow

```
Meta POST /webhook  →  Flask returns 200 immediately
                    │
                    ▼  (daemon thread)
        mark message as read
                    │
        ┌───────────┴───────────┐
        │                       │
   document (PDF receipt)   text message
        │                       │
        ▼                       ▼
  submit_payment_proof()  booking_session.handle_booking_turn()
   → validate receipt      ├─ detect_intent (my_booking / new_booking)
   → confirm booking       ├─ deterministic step machine (Bot 1)
                           └─ on confirm: request_payment()
                                  → reserves slot in Postgres
                                  → Sheets upsert (background)
                                  │
                                  ▼ (fall-through if no booking action)
                           RAG retrieval (ChromaDB)
                                  │
                                  ▼
                           GPT-4o-mini (with tool: start_booking)
                                  │
                                  ▼
                           send reply via Cloud API
```

---

## Project structure

```
bot/
├── app.py                          # Flask routes; registers manager_api; APScheduler jobs
├── config.py                       # All env config; per-bot BOT_CONFIGS dict
├── utils.py                        # now_almaty / today_almaty (timezone-safe)
│
├── handlers/
│   ├── message_handler.py          # Main pipeline (booking check → RAG → LLM → send)
│   ├── booking_session.py          # 6-step booking state machine + i18n RU/KK
│   └── whatsapp_client.py          # Meta Cloud API: send / mark-read / download_media
│
├── integrations/
│   ├── booking_service.py          # Only entry point for booking mutations
│   ├── booking.py                  # Slot generation, free-window math
│   ├── postgres.py                 # Connection pool + read queries + session CRUD
│   ├── receipt_parser.py           # Kaspi/Halyk PDF parser
│   ├── payment_validation.py       # Recipient + amount + date validation
│   └── sheets.py                   # gspread: upsert/refresh/setup
│
├── blueprints/
│   └── manager_api.py              # /api/manager/bookings (GET/POST/PATCH/DELETE)
│
├── chat/
│   ├── llm.py                      # GPT-4o-mini call with start_booking tool
│   ├── conversation.py             # SQLite-backed history with WAL
│   └── system_prompts/
│       ├── sp_1.py                 # Dopshy field rental
│       ├── sp_2.py                 # FS DOPȘÝ school
│       └── sp_3.py                 # Boxy Academy
│
├── rag/
│   ├── vector_store.py             # ChromaDB ingestion + chunking
│   └── retriever.py                # similarity_search with LRU cache
│
├── apps_script/                    # Container-bound manager UI (deployed via clasp)
│   ├── Code.gs / Actions.gs / ApiClient.gs / Setup.gs / Sidebar.gs
│   ├── sidebar.html
│   └── appsscript.json
│
├── migrations/                     # Numbered SQL files (idempotent runner)
│   └── 001…014.sql
├── scripts/
│   ├── migrate.py                  # Migrations runner (tracks schema_migrations)
│   └── ingest.py                   # One-shot CLI to index documents/*.md
│
├── tests/                          # pytest — 132 tests across 8 files
│   ├── conftest.py                 # Auto-migrates + truncates between tests
│   ├── test_booking.py / test_booking_service.py / test_booking_session.py
│   ├── test_concurrency.py         # 50-thread race against EXCLUDE constraint
│   ├── test_payment_validation.py / test_receipt_parser.py
│   ├── test_manager_api.py / test_cancel_intent.py
│
├── documents/                      # Knowledge base (Bot 1 only)
└── receipts/                       # Sample PDFs for parser/validation tests
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Docker + Docker Compose | Recommended path |
| Python 3.12 + Poetry | For local dev |
| OpenAI API key | [platform.openai.com](https://platform.openai.com) |
| Meta Developer account | Three verified WhatsApp phone numbers (one per bot) |
| Google Cloud service account | Optional — only if using Sheets sync |

---

## Quick start (Docker)

```bash
cp .env.example .env             # fill in credentials
docker compose --profile ingest run --rm ingest   # index Bot 1's knowledge base
docker compose up -d             # start bot + Postgres
docker compose logs -f bot
```

Migrations run automatically at startup (`scripts/migrate.py` is invoked by `app.py`).

Expose the bot to Meta via a stable public URL (Cloudflare Tunnel / VPS / Fly.io / ngrok). In Meta Developer Console for each of the three bots:

- **Callback URL**: `https://your-domain/webhook`
- **Verify token**: value of `WHATSAPP_VERIFY_TOKEN`
- Subscribe to **messages**

---

## Local dev (without Docker)

```bash
poetry install
poetry run python scripts/migrate.py           # apply migrations (needs a running Postgres)
poetry run python scripts/ingest.py            # index Bot 1's docs
poetry run python app.py
```

For the test suite (requires a disposable Postgres — tables are truncated between tests):

```bash
POSTGRES_DSN=postgresql://dopshy:changeme@localhost:5432/dopshy POSTGRES_MAX_CONN=60 \
  poetry run pytest
```

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | Yes | OpenAI API key |
| `WHATSAPP_TOKEN` | Yes | Permanent access token from Meta |
| `WHATSAPP_PHONE_NUMBER_ID_BOT_1` | Yes | Phone Number ID for Допши |
| `WHATSAPP_PHONE_NUMBER_ID_BOT_2` | Yes | Phone Number ID for FS DOPȘÝ |
| `WHATSAPP_PHONE_NUMBER_ID_BOT_3` | Yes | Phone Number ID for Boxy Academy |
| `WHATSAPP_VERIFY_TOKEN` | Yes | Webhook verify string (any random value) |
| `POSTGRES_DSN` | Yes | e.g. `postgresql://dopshy:changeme@postgres:5432/dopshy` |
| `POSTGRES_PASSWORD` | Yes (Docker) | Used by the bundled Postgres container |
| `POSTGRES_MAX_CONN` | No | Connection pool size (default 10) |
| `MANAGER_API_KEY` | Yes (mgr) | Long random key shared with Apps Script |
| `MANAGER_RATE_LIMIT` | No | Per-IP rate limit (default 60/min) |
| `KASPI_PAYMENT_URL` | Yes (Bot 1) | Kaspi pay-link sent on booking confirmation |
| `GOOGLE_CREDENTIALS_PATH` | No | Path to service account JSON (default `./secrets/google_credentials.json`) |
| `GOOGLE_SPREADSHEET_ID` | No | Empty → all Sheets calls are silently skipped |
| `GOOGLE_WORKSHEET_NAME` | No | Default `Bookings` |
| `BOOKING_OPEN_TIME` | No | Default `09:00` |
| `BOOKING_CLOSE_TIME` | No | Default `23:00` |
| `BOOKING_SLOT_DURATION` | No | Minutes per slot, default 60 |
| `BOOKING_FIELDS` | No | JSON list: `[{"id":1,"format":"5x5"},…]` |
| `BOOKING_TIMEZONE` | No | Default `Asia/Almaty` |
| `BOOKING_SESSION_TTL` | No | Seconds, default 1800 |
| `PAYMENT_TTL_SECONDS` | No | Slot-reservation window, default 3600 |
| `PAYMENT_MIN_FRACTION` | No | Min share of price required on receipt, default `0.5` |
| `PAYMENT_RECEIPT_MAX_AGE_HOURS` | No | Default 24 |
| `CHROMA_DB_PATH` / `DOCUMENTS_PATH` / `CONVERSATION_DB_PATH` | No | Storage paths |
| `PORT` | No | Default 5000 |

Acceptable payment recipients (Kaspi БИНs and Halyk phone numbers) live in the `payment_recipients` table, not env — manage rows via SQL or the Apps Script Setup dialog.

---

## Booking state machine (Bot 1)

```
new_booking intent (keyword or LLM tool)
  └─ start_booking_flow → create_draft (state=draft) + session at step_date
         │
   step_date    → numbered list of available days
   step_time    → user enters "HH:MM до HH:MM" (RU) or "HH:MM - HH:MM" (KK)
   step_field   → if multiple fields free (auto-skipped otherwise)
   step_players → integer
   step_name    → free text
   step_confirm → да / нет (RU) or иә / жоқ (KK)
         │
   да → request_payment → state=awaiting_payment + reserved_until = now + PAYMENT_TTL_SECONDS
        → EXCLUDE constraint guarantees one winner on race
        → Sheets upsert (background thread)
        → send Kaspi payment link
   нет → cancel_booking → state=cancelled
```

Subsequent **PDF document message** triggers `_handle_payment_receipt`:

1. Download via Graph API → 2. `receipt_parser.parse_receipt` (detects Kaspi / Halyk) →
3. `payment_validation.validate_receipt` (recipient → amount ≥ 50% → date within 24h) →
4. On success: `submit_payment_proof` (UNIQUE `transaction_ref` — reused receipts get `PAYMENT_DUPLICATE`), booking flips to `confirmed`, Sheets refreshed.

Slot reservations that go unpaid past `reserved_until` and abandoned DRAFT rows are swept every 5 minutes by APScheduler in `app.py`, routed through `cancel_booking` so the audit log captures every transition.

Every mutation goes through `integrations/booking_service.py`, which returns a typed `{ok, code, data, message}` envelope and writes a `booking_events` row.

---

## Manager workflow (Google Sheets + Apps Script)

Managers act on bookings inside Google Sheets:

- A container-bound Apps Script (`apps_script/`) adds a menu, a new-booking sidebar, and an `onEdit` trigger that PATCHes cell changes to `manager_api`.
- The backend exposes `/api/manager/bookings` (GET / POST / PATCH / DELETE), guarded by header `X-API-Key: $MANAGER_API_KEY` with an in-process per-IP rate limit.
- The sheet itself is a read-mostly mirror — bookings are computed from Postgres; the manager UI never edits authoritative data directly.

To deploy the Apps Script:

```bash
cd apps_script
clasp login
clasp clone <SCRIPT_ID>        # or `clasp create --type sheets`
clasp push
```

In the script's **Project Settings → Script Properties**, set:

- `API_BASE_URL` — the bot's public URL (e.g. `https://bot.dopshy.kz`)
- `API_KEY` — same value as `MANAGER_API_KEY` on the bot

The first time, run **Допши → Настройка / API-ключ** from the sheet menu to validate.

---

## Knowledge base (Bot 1 only)

Documents in `documents/` are chunked (500 chars, 50 overlap) and indexed into ChromaDB.

| File | Lang | Contents |
|---|---|---|
| `services_ru.md` / `services_kz.md` | RU/KZ | Field types, booking info, hours |
| `pricing_ru.md` | RU | Hourly rates, discounts, cancellation |
| `rules_ru.md` | RU | Field rules, equipment, locker rooms |
| `faq_ru.md` / `faq_kz.md` | RU/KZ | FAQ |

Re-index without restart:

```bash
curl -X POST https://your-domain/admin/ingest \
     -H "X-Admin-Token: $WHATSAPP_VERIFY_TOKEN"
```

This drops and recreates the ChromaDB collection (to avoid duplicate chunks) and invalidates the in-process retriever cache.

---

## API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/webhook` | Meta webhook verification |
| `POST` | `/webhook` | Receive WhatsApp messages |
| `POST` | `/admin/ingest` | Re-index knowledge base (header `X-Admin-Token: $WHATSAPP_VERIFY_TOKEN`) |
| `POST` | `/admin/setup-sheet` | Apply sheet template (header, widths, frozen header, status dropdown) |
| `GET` | `/health` | `{"status": "healthy"}` |
| `GET` | `/api/manager/bookings?from=&to=` | List bookings (manager) |
| `POST` | `/api/manager/bookings` | Create booking (manager) |
| `PATCH` | `/api/manager/bookings/<id>` | Update booking |
| `DELETE` | `/api/manager/bookings/<id>` | Cancel booking |

All `/api/manager/*` endpoints require `X-API-Key: $MANAGER_API_KEY`.

---

## Conversation history & reset commands

The last 20 messages (10 turns) are fed to GPT-4o-mini on every call. History is per `{phone_number_id}:{sender}` so each bot keeps its own thread per user. Reset clears that pair only:

| Command | Lang |
|---|---|
| `/reset` | EN |
| `/сброс` | RU |
| `/тазалау` | KZ |

---

## Testing

132 tests cover the booking service, step machine, payment pipeline, manager API, and a 50-thread concurrency race against the `EXCLUDE` slot constraint.

```bash
POSTGRES_DSN=postgresql://dopshy:changeme@localhost:5432/dopshy POSTGRES_MAX_CONN=60 \
  poetry run pytest
```

`tests/conftest.py` auto-applies migrations and truncates `bookings / booking_events / payments / booking_sessions` between tests.

---

## License

MIT
