# Manager API — Bookings Spec (bot service)

Contract for the booking-related endpoints the bot exposes under `/api/manager/`.
Source of truth: `blueprints/manager_api.py`, `integrations/booking_service.py`,
`integrations/repo/booking_repo.py`. Generated to keep the backend proxy
(`dopshy-backend`) and the bot in sync.

Base URL: `settings.BOT_URL` (e.g. `http://bot:5000`).

---

## 1. Common conventions

### Auth
Every `/api/manager/*` request must send:

```
Content-Type: application/json
Accept: application/json
X-API-Key: <MANAGER_API_KEY>          # header name is exactly "X-API-Key"
```

Failures short-circuit in `before_request`:

| Condition | Status | Body |
|---|---|---|
| Manager API not configured on bot | 503 | `{"ok":false,"code":"NOT_CONFIGURED",...}` |
| Missing/wrong `X-API-Key` | 401 | `{"ok":false,"code":"UNAUTHORIZED","message":"Bad API key."}` |
| Rate limit exceeded (per IP, `MANAGER_RATE_LIMIT`/min) | 429 | `{"ok":false,"code":"RATE_LIMITED",...}` |
| `OPTIONS` (CORS preflight) | 200 | empty (flask-cors) |

CORS: only `/api/manager/*` is browser-facing. Origins from `CORS_ALLOWED_ORIGINS`
(env, comma-separated; `*` = any). Allowed headers: `Content-Type, X-API-Key`.
Methods: `GET, POST, PATCH, DELETE, OPTIONS`.

### Response envelope
```json
{ "ok": true,  "data": <object|array>, "code": "OK",  "message": "" }
{ "ok": false, "code": "<ERROR_CODE>", "data": null,  "message": "<human RU text>" }
```
Success handlers usually return just `{"ok": true, "data": ...}`.

### Serialization rules (`_serialize`, applied to every row)
**These differ from raw SQL types — read carefully:**

| Source type | Serialized as | Example |
|---|---|---|
| `date` | ISO date string | `"2026-07-18"` |
| `datetime` / `timestamptz` | ISO-8601 w/ offset | `"2026-07-16T07:16:29.970441+00:00"` |
| `time` | **`"HH:MM"` (seconds dropped!)** | `23:59:59` → `"23:59"` |
| `Decimal` (money) | `float` | `35000.00` → `35000.0` |
| `uuid` | string | `"cd10…af"` |
| `None` / SQL `NULL` | **`""` (empty string, never JSON `null`)** | |

⚠️ Gotchas for the backend parser:
- **Times are `"HH:MM"`**, not `"HH:MM:SS"`.
- **NULL → `""`.** The bot never emits JSON `null` for a field; it emits `""`.
  Treat `""` as null for optional fields. If a `null` reaches the frontend, it is
  being re-introduced downstream (e.g. a strict `time`-typed field in the backend
  `BotBookingRaw` model coercing `""` back to `None`) — fix that on the backend.

### Status codes
- `200` success · `400` `INVALID` (bad body) · `404` `NOT_FOUND` · `409` create/batch service failure (e.g. `SLOT_TAKEN`).

### Error codes (`code` field)
`INVALID`, `NOT_FOUND`, `SLOT_TAKEN`, `TIME_IN_PAST`, `INVALID_TIME`,
`INVALID_FIELD`, `INVALID_STATE`, `BOOKING_WRONG_STATE`, `NO_CHANGE`,
`PAYMENT_DUPLICATE`, `NO_KASPI`, `PAYMENT_PROVIDER_ERROR`, plus auth codes.

`NO_KASPI` (400, batch only): the `phone` is not registered in Kaspi, so the
avans cannot be pushed to it. Checked **before** anything is inserted — nothing
was created, and the request can be repeated with another number.

### Booking state machine
`draft → awaiting_payment → confirmed`; exits `cancelled`, `unpaid`, `failed`.
Slot overlap enforced by a DB `EXCLUDE` constraint scoped to
`awaiting_payment` + `confirmed` → `SLOT_TAKEN`.

---

## 2. Data models

### `BookingRow` — the **list** endpoints (`/all`, `/`, `/range`)

| Field | Type (serialized) | Notes |
|---|---|---|
| `id` | int | |
| `field` | int | numeric field id (1,2,3) — **never null/empty** on list endpoints (see §4) |
| `date` | `YYYY-MM-DD` | never empty on list endpoints |
| `time_start` | `HH:MM` | never empty on list endpoints |
| `time_end` | `HH:MM` | `23:59` = midnight sentinel (§4) |
| `customer_name` | string (`""` if null) | |
| `phone` | string (`""` if null) | |
| `notes` | string (`""` if null) | |
| `state` | string | see state machine |
| `price_total` | float, or `""` if null | |
| `source` | string | `manager`, `whatsapp`, or the `updated_by` value |
| `reserved_until` | ISO datetime or `""` | TTL for `awaiting_payment` |
| `paid_kaspi_qr` | float | |
| `paid_cash` | float | |
| `created_at` | ISO datetime | |
| `updated_at` | ISO datetime | |
| `group_transition` | uuid string | **links cross-midnight halves** (§4) |
| `paid_avans` | float | avans **requested** at booking time — not money received; do not sum with `paid_api` |
| `paid_api` | number | sum of all accepted payments for the booking (added by endpoint; default `0`) |
| `last_receipt_date` | date/`""` | latest receipt date across payments |

### `BookingDetail` — **GET one** (`/bookings/{id}`)
Same enrichment as the list endpoints — `paid_api`, `last_receipt_date` and the
manual buckets are all present. (Note: GET-one is **not** filtered for
completeness, so it can return a row with empty slot fields if you fetch such an
id directly.)

### `FieldRow` — `GET /fields` → `data.fields[]`
`id`:int, `name`:string, `description`:string(`""`), `format`:string(`"5x5"`/`"6x6"`),
`capacity`:int/`""`. Only `active=true` fields.

### `PriceRow` — `GET /fields` → `data.prices[]`
`format_name`:string, `pricing_type`:one of
`morning_day|evening|late_night|after_midnight|weekend_holiday`, `price_per_hour`:float.

### `CreatedBooking` — create/batch responses
`{ "booking_id": int, "status": "ОЖИДАНИЕ" }` (fixed RU label for `awaiting_payment`).

---

## 3. Endpoints

| # | Method | Path | Body | Success `data` |
|---|---|---|---|---|
| 1 | GET | `/api/manager/bookings/all` | — | `BookingRow[]` |
| 2 | GET | `/api/manager/bookings` (`?from&to`) | — | `BookingRow[]` |
| 3 | GET | `/api/manager/bookings/range/{start}/{end}/{field}` | — | `BookingRow[]` |
| 4 | GET | `/api/manager/bookings/{id}` | — | `BookingDetail` (404 if absent) |
| 5 | GET | `/api/manager/fields` | — | `{prices:PriceRow[], fields:FieldRow[]}` |
| 6 | POST | `/api/manager/bookings` | create body ↓ | `CreatedBooking` |
| 7 | POST | `/api/manager/bookings/batch` | batch body ↓ | `{created: CreatedBooking[]}` |
| 8 | PATCH | `/api/manager/bookings/{id}` | patch body ↓ | `{booking_id:int}` |
| 9 | DELETE | `/api/manager/bookings/{id}` | — | envelope |
| 10 | DELETE | `/api/manager/bookings/all/{id}` | — | envelope (cancels whole group) |
| 11 | POST | `/api/manager/bookings/daily_refresh` | — | `{ok:true}` (ops) |

Lists 2/3/`/` return states `draft, awaiting_payment, confirmed, unpaid`;
`/all` returns all states. `/range` requires a `field` path param; `/` takes
optional `from`/`to` query params (default today … +30d).

### POST `/api/manager/bookings` — create body
```json
{
  "field": 1, "date": "2026-07-20",
  "time_start": "10:00", "time_end": "11:00",   // 24:00 / 23:59 = end of day
  "repeat": "none",                              // none|daily|weekly|monthly
  "end_date": "2026-07-27",                      // required if repeat != none
  "customer": null, "phone": null, "notes": null,
  "price_total": 35000,                          // optional; auto-priced if omitted
  "client_token": "uuid",                        // optional idempotency key
  "reserved_until": 30,                          // optional TTL minutes
  "updated_by": "string"                         // default "Неизвестен"
}
```
Required: `field, date, time_start, time_end` (+`end_date` if repeating).
`400 INVALID` on missing; `409` on `SLOT_TAKEN`/`TIME_IN_PAST`.

### POST `/api/manager/bookings/batch` — batch body (atomic, all-or-nothing)
```json
{
  "slots": [{ "field": 1, "date": "2026-07-20", "time_start": "10:00", "time_end": "11:00" }],
  "customer": null, "phone": null, "notes": null,
  "price_total": 35000, "reserved_until": 30, "updated_by": "string"
}
```
`400 INVALID` if `slots` empty or a slot missing keys. `409 SLOT_TAKEN` if **any**
slot clashes → whole batch rolled back, nothing created. See §4.

### PATCH `/api/manager/bookings/{id}` — patch body (only present keys change)
Accepted keys → column: `field_id`→field, `customer`|`customer_name`→customer_name,
`notes`, `price_total`, `status`→state, `source`, `paid_kaspi_qr`, `paid_cash`,
`time_start`, `time_end`, `date`, `updated_by`. `end_date` is **ignored** for a
single booking. Rescheduling recomputes overlap → `SLOT_TAKEN` on clash.
`404` on service error.

---

## 4. Behavioral notes (important for the backend)

### Data completeness guarantee (list endpoints)
`GET /all`, `GET /`, and `GET /range/...` **exclude never-scheduled rows** — any
row with NULL `field`, `date`, `time_start`, or `time_end`. These come from
abandoned/cancelled WhatsApp conversations that never picked a slot and are not
real bookings. **Guarantee:** every row from a list endpoint has a non-empty
int `field`, `date`, `time_start`, and `time_end`. (Real `cancelled` bookings —
which have full slot data — are still returned by `/all`.)
> This is the root-cause fix for the frontend crash on null `time_start`: those
> rows are no longer returned. `GET /bookings/{id}` is *not* completeness-filtered.

### Midnight & cross-midnight bookings
- The bot cannot store a single row crossing midnight; such a booking is **two
  rows** (one per day) linked by a shared **`group_transition`** UUID.
- End-of-day is `23:59` on the wire (both `24:00` and `23:59` accepted inbound,
  normalized to true midnight). The pre-midnight half stores `…23:59:59` → `"23:59"`.
- **Render a cross-midnight booking as one entity by grouping rows on
  `group_transition`** (shared, non-empty); display `first.time_start – second.time_end`.
- Every booking has a `group_transition`; a single booking is a group of one.
  Only rows that **share** the value are the two halves of one booking.

### Batch slot handling
- Overlapping/adjacent same-field slots are **merged** before creation (incl.
  across midnight via the `23:59/24:00` sentinel).
- Batch is **atomic** (one transaction). Any overlap violation rolls back the
  whole batch → `SLOT_TAKEN`, nothing created.

### Fields returned vs. not
- List rows do **not** include `format`. Use `GET /fields` → `fields[].format`
  keyed by field id if needed.
- `GET /bookings/{id}` returns the reduced `BookingDetail` shape (§2).

---

## 5. Open alignment items (bot ↔ backend)
1. **Times are `"HH:MM"`, not `"HH:MM:SS"`** as the backend spec assumed. Confirm
   `BotBookingRaw` parses `"HH:MM"` (and `""`).
2. **NULL → `""`, never JSON `null`.** The bot already coerces this; if `null`
   reaches the frontend, the backend model is re-introducing it — align there.
3. `GET /bookings/{id}` returns fewer fields (no `paid_*`, `reserved_until`,
   `updated_at`, `payment_current`) than the list endpoints. Widen if needed.
4. `price_total`/`last_receipt_date`/`reserved_until` come back as `""` when null;
   the backend spec wants `price_total` as `"0"`. Decide on one and we'll match.
5. Money values are JSON `float` (from `Decimal`). Confirm precision handling.
