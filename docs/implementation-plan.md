# Implementation Plan — DB Reliability Core + Apps Script Manager UI

Unifies **spec-01** (booking state machine & reliability layer) and **spec-02** (Google
Apps Script manager UI), expressed as a delta from the current codebase.

## Locked Decisions

| # | Decision | Overrides |
|---|---|---|
| 1 | **PostgreSQL is the single source of truth.** Managers reach the DB through the Apps Script `manager_api` (Sheet → DB). The bot no longer reads the sheet for availability. | "Sheets is source of truth" framing |
| 2 | **Keep the deterministic step machine** in `handlers/booking_session.py` (no LLM extraction during the flow). Back it with the new state machine, locking, audit, and idempotency. | spec-01's implied LLM extraction; old `booking_integration_plan.md` |
| 3 | **psycopg2 + numbered SQL migration files.** No SQLAlchemy, no Alembic. | spec-01's ORM stack |
| 4 | **SERIAL integer booking PK.** Idempotency comes from `client_token`, not the PK. Apps Script passes the SERIAL id in URLs — works fine. | spec UUID |
| 5 | **Flat one-row-per-booking "Bookings" sheet** (spec-02 layout). Replaces the per-field weekly grid. | current per-field grid |
| 6 | **DRAFT row created at flow start.** `booking_sessions.booking_id` populated from the beginning. State path: DRAFT → AWAITING_PAYMENT → CONFIRMED / CANCELLED / FAILED. | current "row only at confirm" |
| 7 | **1-hour payment reservation TTL, 5-minute sweep interval.** | spec-01's 10-min TTL / 60-s sweep |

## Resolved Risks

1. **`btree_gist`** — available out of the box in `postgres:16-alpine` (official image bundles contrib). No docker-compose change needed.
2. **Abandoned DRAFT rows** — the sweeper also cancels DRAFT rows older than `BOOKING_SESSION_TTL`.
3. **Apps Script** — container-bound to the bookings spreadsheet.
4. **`proof_url`** — store the raw WhatsApp media ID (no Graph API round-trip; avoids expiring download URLs). PDF verification is stubbed OK per spec-01.
5. **`fields` seed** — seeded from the `BOOKING_FIELDS` env JSON inside `scripts/migrate.py` on first run. No per-environment INSERT migrations.

---

## Current State vs. What the Specs Add

Substantially built already: the deterministic 6-step state machine (`booking_session.py`),
Postgres session + booking CRUD (`postgres.py`), the 5-minute sweeper (`app.py`), the
payment-receipt handler (`message_handler.py`), and the per-field weekly-grid Sheets writer
(`sheets.py`).

The specs add: (1) hardened concurrency via a `state` enum + an `EXCLUDE` constraint replacing
`UNIQUE(date,time_start,field)`, (2) a `booking_events` audit trail, (3) a `client_token`
idempotency key, (4) DRAFT row creation at flow start, (5) a flat "Bookings" sheet replacing
the per-field grid, and (6) a manager-facing Flask API + Apps Script sidebar.

---

## Target Data Model

### `bookings` — changes from current schema

| Column | Change | Notes |
|---|---|---|
| `id` | keep `SERIAL` PK | Apps Script `booking_id` column holds the SERIAL int |
| `state` | add `VARCHAR(20) NOT NULL DEFAULT 'draft'` | `draft` / `awaiting_payment` / `confirmed` / `cancelled` / `failed` |
| `status` | drop after migration | values translated into `state` first |
| `start_at` | add `TIMESTAMPTZ` | from `date + time_start` at `Asia/Almaty` |
| `end_at` | add `TIMESTAMPTZ` | from `date + time_end` at `Asia/Almaty` |
| `client_token` | add `UUID NOT NULL DEFAULT gen_random_uuid()`, `UNIQUE` | idempotency key |
| `reserved_until` | add `TIMESTAMPTZ` | set to `now() + 1h` on entering `awaiting_payment` |
| `source` | add `VARCHAR(20) NOT NULL DEFAULT 'whatsapp'` | `whatsapp` / `manager` |
| `price_total` | add `NUMERIC(10,2)` | nullable |
| `UNIQUE(date,time_start,field)` | drop | replaced by EXCLUDE |
| EXCLUDE constraint | add | `EXCLUDE USING gist (field WITH =, tstzrange(start_at, end_at, '[)') WITH &&) WHERE (state NOT IN ('cancelled','failed'))` |

### New tables

**`fields`** — reference table (id, name, format, capacity); seeded from `BOOKING_FIELDS` JSON in `migrate.py`.

**`booking_events`** (audit log):
```
id BIGSERIAL PK, booking_id INTEGER REFERENCES bookings, event VARCHAR(40),
actor_type VARCHAR(20), actor_id TEXT, note TEXT, created_at TIMESTAMPTZ DEFAULT now()
```

**`payments`**:
```
id SERIAL PK, booking_id INTEGER REFERENCES bookings, method VARCHAR(20),
proof_media_id TEXT, verified_by TEXT, verified_at TIMESTAMPTZ,
amount NUMERIC(10,2), created_at TIMESTAMPTZ DEFAULT now()
```

**`booking_sessions`** — no schema change; the existing `booking_id` FK is now populated from
`start_booking_flow()` instead of staying NULL until confirm.

### Migration step order (`migrations/`)

1. `001_add_state_columns.sql` — add `start_at`, `end_at`, `client_token`, `reserved_until`, `source`, `price_total`, `state` (nullable initially).
2. `002_backfill.sql` — `UPDATE` `start_at`/`end_at` from `(date + time_*) AT TIME ZONE 'Asia/Almaty'`; map `status` → `state` (`awaiting_payment`→`awaiting_payment`, `paid`→`confirmed`, `cancelled`→`cancelled`, `completed`→`confirmed`).
3. `003_constraints.sql` — `state SET NOT NULL`; add `UNIQUE(client_token)`.
4. `004_gist.sql` — `CREATE EXTENSION IF NOT EXISTS btree_gist`; add the EXCLUDE constraint.
5. `005_drop_old.sql` — drop `UNIQUE(date,time_start,field)`; drop `status` column.
6. `006_new_tables.sql` — create `fields`, `booking_events`, `payments`.

### Migration runner

`scripts/migrate.py` — tracks applied files in a `schema_migrations(filename, applied_at)`
table, applies `migrations/*.sql` in filename order (one transaction per file), seeds `fields`
from `BOOKING_FIELDS` on first run. Called at `app.py` startup (replaces `init_schema()`) and
runnable via `poetry run python scripts/migrate.py`.

---

## Phased Plan

### Phase 1 — Schema migration + runner
**Files:** `migrations/001–006_*.sql`, `scripts/migrate.py`, `integrations/postgres.py`, `app.py`
- Write the 6 SQL files and the runner; seed `fields` from `BOOKING_FIELDS`.
- Replace the `init_schema()` startup call with `migrate()`.

### Phase 2 — Service layer
**Files:** `integrations/booking_service.py` (new), `tests/test_booking_service.py` (new)

Six functions, all returning `{"ok": bool, "code": str, "data": dict|None, "message": str}`:

| Function | Behaviour |
|---|---|
| `create_draft(...)` | INSERT `state='draft'`; event `draft_created` |
| `update_draft(booking_id, **patch)` | UPDATE allowed fields while `state='draft'`; event `draft_updated` |
| `request_payment(booking_id, client_token)` | `UPDATE … state='awaiting_payment', reserved_until=now()+1h WHERE state='draft' AND client_token=… RETURNING id`; EXCLUDE rejects overlaps → `code='conflict'`; event `payment_requested` |
| `submit_payment_proof(booking_id, proof_media_id)` | insert `payments` row, `state='confirmed'`; verification stubbed OK; event `payment_received` |
| `cancel_booking(booking_id, actor_type, actor_id, reason)` | `state='cancelled'` if not terminal; event `cancelled` |
| `manager_create_booking(...)` | INSERT directly `state='confirmed'`, `source='manager'`; event `manager_created` |

Raw psycopg2 via the existing `_conn()` context manager. Tests run against a real test Postgres.

### Phase 3 — Sweeper upgrade
**Files:** `app.py`, `integrations/booking_service.py`
- Predicate: cancel `awaiting_payment` rows where `reserved_until < now()`, **and** `draft` rows older than `BOOKING_SESSION_TTL`.
- Route cancellations through `cancel_booking()` so each writes a `booking_events` row.
- Keep the 5-minute APScheduler interval. WhatsApp expiry notifications stay.

### Phase 4 — Concurrency test
**Files:** `tests/test_concurrency.py` (new)
- 50 threads call `request_payment` for the same slot → assert exactly one `ok=True`, rest `code='conflict'`.

### Phase 5 — Wire `booking_session.py` to the service layer
**Files:** `handlers/booking_session.py`
- `start_booking_flow()` → `create_draft()` immediately; store `booking_id` in session + `booking_sessions.booking_id`.
- Each `_handle_step_*` → `update_draft()` as params are collected.
- `_confirm_booking()` → `request_payment()` instead of `create_booking()`; on `conflict` show the existing "slot taken" message.

### Phase 6 — Payment receipt upgrade
**Files:** `handlers/message_handler.py`
- `_handle_payment_receipt()` → `submit_payment_proof(booking_id, proof_media_id=<whatsapp media id>)`.
- PDF verification stubbed OK. Sheet update triggered (Phase 7).

### Phase 7 — Flat-table `sheets.py` rewrite
**Files:** `integrations/sheets.py`, `integrations/booking.py`, `app.py`

Single "Bookings" worksheet, columns A–I:
`booking_id | field | date | start | end | customer | notes | status | last_synced`

| Old | New | Change |
|---|---|---|
| `append_booking` | `append_or_update_booking` | find row by booking_id in col A; update or append |
| `update_booking_status_in_sheet` | `update_booking_row` | look up by booking_id, update changed cells |
| `maybe_refresh_week` | `refresh_all_bookings` | clear + rewrite from `SELECT … WHERE state!='cancelled' ORDER BY start_at` |
| `setup_sheet_template` | `setup_sheet_template` | adapt to 9 columns; keep status dropdown |
| `get_booked_slots` | **delete** | remove from availability path |

- `booking.get_all_booked()` → return only `postgres.get_booked_slots()` (drop the Sheets merge).
- Stop writing `sheets_sync_state` (leave the table one release; remove later).
- Monday APScheduler job and `/admin/setup-sheet` call the new functions.

### Phase 8 — `manager_api` Flask blueprint
**Files:** `blueprints/manager_api.py` (new), `app.py`

Auth: `X-API-Key` vs `config.MANAGER_API_KEY`. Rate limit 60/min per IP.

| Endpoint | Behaviour |
|---|---|
| `GET /api/manager/bookings?date=&field=&state=` | list bookings |
| `GET /api/manager/bookings/<id>` | single booking + event history |
| `POST /api/manager/bookings` | `manager_create_booking()` |
| `PATCH /api/manager/bookings/<id>` | update `notes` / `state` / `price_total`; writes event |
| `DELETE /api/manager/bookings/<id>` | `cancel_booking(actor_type='manager', …)` |

Response envelope mirrors the service layer.

### Phase 9 — Apps Script project (container-bound)
**Files:** `apps_script/` (committed, deployed via `clasp`)

| File | Purpose |
|---|---|
| `Code.gs` | `onOpen()` menu; `onEdit(e)` for F=customer / G=notes → PATCH with revert-on-failure |
| `ApiClient.gs` | `UrlFetchApp` wrapper with `X-API-Key` from Script Properties |
| `Sidebar.gs` + `sidebar.html` | new-booking sidebar; client-side validation; `google.script.run` → `submitNewBooking` |
| `Actions.gs` | `cancelSelectedRow()` and helpers |
| `Setup.gs` | `showSetupDialog()` stores `API_BASE_URL` / `API_KEY` |
| `appsscript.json` | manifest, OAuth scopes (`spreadsheets`, `script.external_request`) |

---

## Cross-Cutting

### New `.env`
| Variable | Purpose | Default |
|---|---|---|
| `MANAGER_API_KEY` | auth for `/api/manager/*` | — (required) |
| `MANAGER_RATE_LIMIT` | requests/min per IP | `60` |

Optional dependency: `flask-limiter` for Phase 8. No other new Python deps.

### To remove
- `sheets.get_booked_slots()` and its call in `booking.get_all_booked()` (Phase 7)
- per-field weekly-grid logic in `sheets.py` (Phase 7)
- `create_booking()` call in `_confirm_booking()` (Phase 5)
- `update_booking_status()` call in the payment handler (Phase 6)
- `sheets_sync_state` writes (Phase 7; table dropped a release later)

### Acceptance criteria → tests
| Criterion | Test |
|---|---|
| Concurrent `request_payment` → exactly 1 OK | `tests/test_concurrency.py` |
| Service-fn envelopes correct | `tests/test_booking_service.py` |
| Sweeper cancels only expired `awaiting_payment` + stale `draft` | `tests/test_booking_service.py` |
| `manager_create_booking` inserts confirmed | `tests/test_booking_service.py` |
| `get_all_booked` no longer reads Sheets | `tests/test_booking.py` |
| Manager API rejects wrong key; PATCH writes event | `tests/test_manager_api.py` |
| Flat sheet refresh writes 9-col rows | `tests/test_sheets.py` |
