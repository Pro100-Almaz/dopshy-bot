# Booking Integration Plan: Google Sheets + PostgreSQL

**Design principle: zero recurring admin actions.** After a one-time setup, the system runs fully automatically. Users book through WhatsApp, the bot writes to both PostgreSQL and Google Sheets, and the weekly schedule view refreshes itself.

---

## Role of Each System

| System | Role | Who writes | Who reads |
|--------|------|------------|-----------|
| **PostgreSQL** | Single source of truth — all bookings, sessions, full history | Bot only | Bot only |
| **Google Sheets** | Admin-facing schedule view for the current week | Bot only | Admin (read-only) |
| **WhatsApp** | User-facing interface for booking, querying, paying | Users | Users |

The admin never needs to write to Sheets. It is purely a display layer the bot keeps up to date.

---

## Automated Flows (no human involvement)

### 1. User books a field (conversational, multi-turn)

```
User: "хочу забронировать поле"
Bot:  shows free slots for the week, asks for details

User: "пятницу в 19:00, 5x5"
Bot:  extracts date/time/format, confirms player count if not given

User: "10 человек, меня зовут Almaz"
Bot:  shows summary → "Поле 1 (5x5), Пт 10.04 19:00–20:00, 10 игроков. Подтвердить?"

User: "да"
Bot:  writes booking to PostgreSQL (status: awaiting_payment)
      writes row to Google Sheets
      sends Kaspi payment link
```

### 2. User pays (document receipt)

```
User: [sends document / image of receipt]
Bot:  looks up most recent awaiting_payment booking for this phone in PostgreSQL
      marks status → paid in PostgreSQL
      updates row in Google Sheets
      sends personalized confirmation with booking details
```

This replaces the current hardcoded "payment accepted" message with a context-aware response.

### 3. User queries availability

```
User: "когда есть свободные часы?"
Bot:  generates all possible slots from config (open/close time, duration, fields)
      queries PostgreSQL for booked slots this week
      free = all_slots − booked_slots
      formats and replies
```

No Sheets read needed — PostgreSQL is truth.

### 4. User queries their booking

```
User: "когда моя игра?"
Bot:  queries PostgreSQL by sender phone
      returns upcoming bookings for this user
```

### 5. Weekly Sheets refresh (automated, no cron daemon needed)

On the first message processed after Monday 00:00 (Almaty time, UTC+5), the bot:
1. Clears the current week's rows from the `Текущая неделя` worksheet.
2. Writes all `booked` / `awaiting_payment` rows for the new week from PostgreSQL.

This is a lazy trigger — no external scheduler required. A background thread handles it so it doesn't block the webhook response.

### 6. User cancels a booking

```
User: "хочу отменить бронь"
Bot:  shows user's upcoming bookings
User: picks the one to cancel
Bot:  updates status → cancelled in PostgreSQL
      removes/strikes row in Google Sheets
      confirms cancellation and refund policy
```

---

## Booking Session State Machine

Stored in PostgreSQL `booking_sessions` table. Each user can have one active session at a time.

```
idle
  │ booking intent detected
  ▼
collecting  ◄──── LLM extracts params from each message, asks for missing ones
  │ all of: date, time, format, players collected
  ▼
confirming  ── user says no/change ──► collecting
  │ user confirms
  ▼
awaiting_payment  ── payment link sent
  │ user sends document
  ▼
completed  (session closed, booking record stays in bookings table)
```

**LLM-driven extraction:** Rather than a rigid keyword flow, the bot passes the user's message plus the current partial session data to GPT-4o-mini with a structured extraction prompt. GPT returns a JSON object with whatever fields it could extract. Missing fields trigger a natural-language follow-up question. This handles "забронируй пятницу 19:00 поле 5x5 на 10 человек" (all in one message) as gracefully as a multi-turn conversation.

---

## PostgreSQL Schema

```sql
-- Confirmed and in-progress bookings (permanent history)
CREATE TABLE bookings (
    id            SERIAL PRIMARY KEY,
    phone         VARCHAR(20)   NOT NULL,
    customer_name VARCHAR(100),
    date          DATE          NOT NULL,
    time_start    TIME          NOT NULL,
    time_end      TIME          NOT NULL,
    field         SMALLINT      NOT NULL,
    format        VARCHAR(5)    NOT NULL,   -- '5x5' | '6x6'
    players       SMALLINT,
    status        VARCHAR(20)   NOT NULL DEFAULT 'awaiting_payment',
                                           -- awaiting_payment | paid | cancelled | completed
    sheet_row     INTEGER,                 -- row index in Google Sheet (for targeted updates)
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    notes         TEXT,
    UNIQUE (date, time_start, field)
);

CREATE INDEX idx_bookings_phone  ON bookings (phone);
CREATE INDEX idx_bookings_date   ON bookings (date);
CREATE INDEX idx_bookings_status ON bookings (status);

-- In-progress booking conversations
CREATE TABLE booking_sessions (
    chat_id       TEXT          PRIMARY KEY,  -- "{phone_number_id}:{sender_id}"
    state         VARCHAR(20)   NOT NULL,     -- collecting | confirming | awaiting_payment
    params        JSONB         NOT NULL DEFAULT '{}',
                                              -- collected so far: date, time, format, players, name
    booking_id    INTEGER       REFERENCES bookings(id),
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW() + INTERVAL '30 minutes'
);

-- Track last Sheets sync per week (for lazy weekly refresh)
CREATE TABLE sheets_sync_state (
    id            SERIAL PRIMARY KEY,
    week_start    DATE          NOT NULL UNIQUE,  -- Monday of the synced week
    synced_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
```

---

## Google Sheets Structure

The bot writes and maintains this. Admin opens it to view the schedule.

**Spreadsheet:** one sheet named `Текущая неделя`.

| Col | Header | Example |
|-----|--------|---------|
| A | Дата | 10.04.2026 (Пт) |
| B | Начало | 19:00 |
| C | Конец | 20:00 |
| D | Поле | 1 |
| E | Формат | 5x5 |
| F | Игроков | 10 |
| G | Клиент | Almaz |
| H | Телефон | 77771234567 |
| I | Статус | ✅ Оплачено / ⏳ Ожидает оплату / ❌ Отменено |
| J | Примечание | — |

**The service account needs Editor (write) access** — not just Viewer. The bot:
- Appends a row when a booking is created
- Updates the status cell (column I) when payment is confirmed or booking cancelled
- Clears and rewrites the sheet on the first message of a new week

---

## New Modules

| Module | Responsibility |
|--------|---------------|
| `integrations/postgres.py` | Connection pool, schema init, CRUD for `bookings` and `booking_sessions`, queries (free slots, user bookings, active session) |
| `integrations/sheets.py` | Authenticate with service account, append row, update cell, clear+rewrite sheet, lazy weekly refresh trigger |
| `integrations/booking.py` | Business logic: generate all slots from config, compute free slots, format context for LLM, orchestrate booking lifecycle |
| `handlers/booking_session.py` | Session state machine: detect intent, drive multi-turn LLM extraction, transition states, call booking.py actions |

## Updated Modules

| Module | Change |
|--------|--------|
| `config.py` | Add PostgreSQL DSN, Google Sheets vars, operating hours, slot duration, field definitions |
| `handlers/message_handler.py` | Before RAG: check for active booking session → route to `booking_session.py`. After RAG for non-booking messages: unchanged |
| `chat/llm.py` | Add `extract_booking_params()` function using structured output (JSON mode) for the session state machine |
| `app.py` | Call `postgres.init_schema()` at startup |

### Updated message flow

```
Incoming message (Bot 1 only)
  │
  ├─ document? → look up awaiting_payment booking by phone
  │              → mark paid in PostgreSQL + Sheets → personalized confirmation
  │
  ├─ active booking session? → booking_session.py handles entire turn
  │                            (no RAG, no regular LLM call)
  │
  ├─ booking/availability/my-booking intent?
  │     → booking_session.py (starts new session or answers read-only query)
  │
  └─ else → existing pipeline unchanged (RAG → LLM → reply)
```

---

## Slot Generation Logic

Slots are never stored as "free" in the database — only booked slots exist. Free = generated range minus booked.

```python
# Config-driven slot grid
# BOOKING_FIELDS = [{"id": 1, "format": "5x5"}, {"id": 2, "format": "6x6"}]
# BOOKING_OPEN_TIME = "09:00", BOOKING_CLOSE_TIME = "23:00"
# BOOKING_SLOT_DURATION = 60  (minutes)

all_slots = [
    (date, time_start, field_id, field_format)
    for date in week_dates          # Mon–Sun of current week
    for time_start in time_range    # 09:00, 10:00, … 22:00
    for field in BOOKING_FIELDS
]

booked = booking_repo.get_booked_slots(week_start, week_end)
free_slots = [s for s in all_slots if s not in booked]
```

---

## LLM Booking Parameter Extraction

A dedicated prompt (not the main system prompt) is used during the `collecting` state:

```
You are extracting booking parameters from a WhatsApp message.
Current collected params: {params_json}
User message: "{user_text}"
Available slots summary: {free_slots_summary}

Return JSON with any newly extracted fields. Use null for unknown fields.
{
  "date": "YYYY-MM-DD or null",
  "time_start": "HH:MM or null",
  "format": "5x5 | 6x6 | null",
  "players": integer or null,
  "customer_name": "string or null"
}
```

If all fields are non-null, transition to `confirming`. Otherwise, the bot asks a natural-language follow-up generated by a second LLM call.

---

## Environment Variables

Add to `.env` and `.env.example`:

```env
# PostgreSQL
POSTGRES_DSN=postgresql://dopshy:password@postgres:5432/dopshy
POSTGRES_PASSWORD=changeme

# Google Sheets
GOOGLE_CREDENTIALS_PATH=./secrets/google_credentials.json
GOOGLE_SPREADSHEET_ID=<id_from_sheet_url>
GOOGLE_WORKSHEET_NAME=Текущая неделя

# Booking (Bot 1 only)
BOOKING_OPEN_TIME=09:00
BOOKING_CLOSE_TIME=23:00
BOOKING_SLOT_DURATION=60
BOOKING_FIELDS=[{"id":1,"format":"5x5"},{"id":2,"format":"6x6"}]
BOOKING_TIMEZONE=Asia/Almaty
BOOKING_SESSION_TTL=1800        # seconds before an idle session expires (30 min)
```

---

## New Dependencies

```toml
gspread = "^6.0"
google-auth = "^2.0"
psycopg2-binary = "^2.9"
```

---

## Docker Compose Changes

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: dopshy
      POSTGRES_USER: dopshy
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U dopshy"]
      interval: 10s
      timeout: 5s
      retries: 5

  bot:
    ...
    volumes:
      - ...existing volumes...
      - ./secrets:/app/secrets:ro    # Google service account JSON
    depends_on:
      postgres:
        condition: service_healthy

volumes:
  postgres_data:
```

---

## Implementation Steps

### Step 1 — PostgreSQL foundation
- Add `POSTGRES_DSN` to `config.py`
- Create `integrations/postgres.py`: `ThreadedConnectionPool`, `init_schema()` (creates all 3 tables), CRUD helpers
- Call `init_schema()` from `app.py` at startup

### Step 2 — Slot logic & availability queries (no Sheets yet)
- Add booking config vars to `config.py`
- Create `integrations/booking.py`: `generate_all_slots()`, `get_free_slots()`, `get_user_bookings()`, `format_booking_context()`
- Wire availability + my-booking read-only queries into `message_handler.py` (keyword detection → inject context → LLM replies as before)
- **Testable**: bot can answer availability questions using only PostgreSQL

### Step 3 — Booking session state machine
- Add `extract_booking_params()` to `chat/llm.py` (JSON mode call)
- Create `handlers/booking_session.py`: session CRUD, state transitions, LLM extraction loop, confirmation flow
- Wire into `message_handler.py`: check for active session before existing pipeline
- **Testable**: full booking conversation flow, booking written to PostgreSQL, Kaspi link sent

### Step 4 — Payment confirmation update
- Update the `msg_type == "document"` branch in `message_handler.py`
- Look up most recent `awaiting_payment` booking for the sender phone
- If found: mark paid in PostgreSQL, send personalized confirmation with booking details
- If not found: fall back to existing generic message

### Step 5 — Google Sheets integration
- Create `integrations/sheets.py`: service account auth, `append_booking()`, `update_booking_status()`, `refresh_week()`
- Hook into booking lifecycle: append on creation, update on payment/cancellation
- Add lazy weekly refresh to `refresh_week()` called from the background thread

### Step 6 — Docker, secrets, env
- Update `docker-compose.yml` with PostgreSQL service and secrets volume mount
- Add `secrets/` to `.gitignore`
- Update `.env.example` with all new variables

---

## One-Time Setup (only human actions ever needed)

1. Create a Google Cloud project and enable the Sheets API.
2. Create a service account, download JSON key → save to `secrets/google_credentials.json`.
3. Create a Google Sheet, name the first tab `Текущая неделя`, share it with the service account email as **Editor**.
4. Copy the spreadsheet ID into `.env`.
5. Set PostgreSQL credentials in `.env`.
6. Deploy.

After this, no recurring admin actions are required. The admin's only ongoing interaction with the system is opening the Google Sheet to view the schedule.

---

## Security Notes

- `secrets/google_credentials.json` must be in `.gitignore`. Mount into Docker as a read-only bind mount, never bake into image.
- PostgreSQL password in `.env` only — never hardcoded.
- Service account has Editor access on the spreadsheet only, with no access to other Google resources.
- Booking sessions expire after `BOOKING_SESSION_TTL` seconds to prevent ghost sessions from stale conversations.
