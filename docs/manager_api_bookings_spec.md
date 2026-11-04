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

`NO_KASPI` (400, `/bookings/{id}/invoice` only): the payment number is not
registered in Kaspi. The batch no longer returns it — see §6 (payment by link).

`CONSENT_REQUIRED` (400 on batch, 409 on `/bookings/{id}/invoice`): the client
has not agreed to the public offer, so no avans invoice is raised. See §6.

### Booking state machine
`draft → [awaiting_consent →] awaiting_payment → confirmed`; exits `cancelled`,
`unpaid`, `failed`. `awaiting_consent` (ApiPay on) = slot held, client being
asked on WhatsApp to agree to the offer and for a Kaspi number; **no invoice
exists yet**. Slot overlap enforced by a DB `EXCLUDE` constraint scoped to
`awaiting_consent` + `awaiting_payment` + `confirmed` → `SLOT_TAKEN`.

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
| `kaspi_phone` | string/`""` | number the avans invoice was sent to (`8XXXXXXXXXX`) |
| `consent_at` | ISO datetime/`""` | when the client agreed to the offer — card shows «Офертамен келісті: [date, time]» |
| `consent_channel` | `bot`/`site`/`admin_bot`/`""` | where they agreed (`admin_bot` = manager-created, agreed in WhatsApp) |
| `consent_text_version` | string/`""` | wording of the consent question (`CONSENT_TEXT_VERSION`) |
| `offer_version` | string/`""` | offer they agreed to (`OFFER_VERSION`) |
| `payment_method` | `kaspi_invoice`/`link`/`""` | how the avans is collected (`link` = confirm by receipt) |

### `BookingDetail` — **GET one** (`/bookings/{id}`)
Same enrichment as the list endpoints — `paid_api`, `last_receipt_date` and the
manual buckets are all present. (Note: GET-one is **not** filtered for
completeness, so it can return a row with empty slot fields if you fetch such an
id directly.) Additionally: `apipay_invoice_id` (ApiPay's id of the latest
invoice covering the booking, `""` if none) and `consent_step` (`offer`,
`same_number`, `new_number`, `link`, `done`, `declined`, `expired`,
`cancelled`, or `""`) — `link` means the client pays by link and an admin must
confirm the payment.

### `FieldRow` — `GET /fields` → `data.fields[]`
`id`:int, `name`:string, `description`:string(`""`), `format`:string(`"5x5"`/`"6x6"`),
`capacity`:int/`""`. Only `active=true` fields.

### `PriceRow` — `GET /fields` → `data.prices[]`
`format_name`:string, `pricing_type`:one of
`morning_day|evening|late_night|after_midnight|weekday_special`, `price_per_hour`:float.

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
  "reserved_until": 60,                          // optional TTL minutes
  "updated_by": "string"                         // default "Неизвестен"
}
```
Required: `field, date, time_start, time_end` (+`end_date` if repeating).
`400 INVALID` on missing; `409` on `SLOT_TAKEN`/`TIME_IN_PAST`.

With ApiPay on, a **non-repeating** booking **with a `phone`** is created as
`awaiting_consent` (not `confirmed`) and the client is asked on WhatsApp to agree
to the offer; the avans invoice follows their consent (§6). Optional `lang`
(`ru`|`kk`, default `ru`) picks the language of that question. Repeating or
phone-less bookings are created `confirmed` as before.

### POST `/api/manager/bookings/batch` — batch body (atomic, all-or-nothing)
```json
{
  "slots": [{ "field": 1, "date": "2026-07-20", "time_start": "10:00", "time_end": "11:00" }],
  "customer": null, "phone": null, "notes": null,
  "price_total": 35000, "reserved_until": 60, "updated_by": "string"
}
```
`400 INVALID` if `slots` empty or a slot missing keys. `409 SLOT_TAKEN` if **any**
slot clashes → whole batch rolled back, nothing created. See §4.

**Avans (ApiPay on, ≥1 non-repeating slot)** — never invoiced without consent:
- **Manager** (no `consent`): bookings are created `awaiting_consent`; nothing is
  checked in Kaspi or invoiced. Response: `invoice: null`,
  `consent: {status: "pending", request_id}`. The bot asks the client (interactive
  message, or the Meta template if they have been silent 24h+); the invoice is
  raised after «Келісемін». `phone` is required. Optional `kaspi_phone` (payment
  number, default `phone`; `400 INVALID` if malformed), `lang`, `prepayment`
  (overrides the invoice total, as before).
- **Website** (`consent` present, or `channel: "site"`): body adds
  `consent: {accepted: true, offer_version?, text_version?}` and `kaspi_phone?`
  (default `phone`). `consent.accepted !== true` → `400 CONSENT_REQUIRED`, nothing
  created. Otherwise the consent is stored on every booking (`consent_channel=site`),
  `kaspi_phone` is checked and the invoice goes out at once; without Kaspi the
  booking is paid by link instead (§6). See `docs/offer_frontend_spec.md`.

### POST `/api/manager/bookings/{id}/invoice` — admin: bill a held booking
Body `{kaspi_phone?}` (default: the number last offered, else the client's own).
For billing a corrected number after a link fallback (`payment_method = link`).
Only for a booking whose client **already agreed**:
`409 NOT_AWAITING_CONSENT` / `409 CONSENT_REQUIRED` otherwise — there is no
force. `400 NO_KASPI`, `400 INVALID`, `502 PAYMENT_PROVIDER_ERROR` (bookings
released). `200 {ok, invoice, booking_ids}`; the client is told on WhatsApp.

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

### 24-hour administrative hold without payment

`POST /api/manager/bookings` and `POST /api/manager/bookings/batch` accept
`"hold_without_payment": true`. The server forces `awaiting_payment`,
`reserved_until = NOW() + 24 hours`, `paid_avans = 0`, and
`payment_method = "hold_without_payment"`. It does not require a phone and
does not check Kaspi, create an ApiPay invoice, or accept a client consent.
The normal TTL sweep releases the slot after 24 hours.

To collect payment later, call
`POST /api/manager/bookings/{id}/request-payment` with optional `phone`,
`kaspi_phone`, and `lang`. A phone must exist by that point. This changes the
hold to `awaiting_consent` and starts the ordinary manager WhatsApp
confirmation/offer flow; it never creates an invoice before consent.

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

---

## 6. Offer consent before the ApiPay invoice

No ApiPay invoice is raised for a booking until its `consent_at` is set — enforced
in `apipay_service.queue_invoice`, which every booking invoice passes through.
Dialog: `integrations/consent_flow.py`; schema: migration `063_offer_consent.sql`;
WhatsApp templates: `docs/whatsapp_templates.md`.

| Path | Consent | Invoice |
|---|---|---|
| Bot «Подтверждаю» | separate «✅ Келісемін / ❌ Жоқ» message; `consent_channel=bot` | after consent |
| Manager batch / single | bot asks the client (template `booking_confirm_request` if silent 24h+); `admin_bot` | after consent, to the manager's `kaspi_phone` |
| Website | checkbox → `consent` in the request; `site` | immediately |
| Contract instalments | **not covered** (open question) | on due date, as before |

Two numbers: `phone` = contact (WhatsApp), `kaspi_phone` = payment number
(default `phone`). Bot: the manager's `kaspi_phone` if given, else
`customers.kaspi_phone` (last number billed successfully, asked «Шотты
өткендегідей … жіберейін бе?»), else the WhatsApp number.

Payment number without Kaspi (bot dialog) → «Бұл нөмірде Kaspi табылмады. Төлем
шотын қай нөмірге жіберейін? …» — the typed number is format-checked (bad format
re-asked, not counted) and tried; at most `KASPI_PHONE_MAX_RETRIES` (2) asks. Still
no Kaspi after that, ApiPay unable to check, or a website payment number without
Kaspi → **no refusal**: the client is told «Администратор сізбен байланысады»,
bookings go `awaiting_payment` with `payment_method = link` for
`LINK_PAYMENT_TTL_SECONDS`, the client gets `KASPI_PAYMENT_URL`, and
`ADMIN_ALERT_PHONE` gets an alert to confirm the payment by its receipt. The
website response carries `payment: {method: "link", url, amount, reason}`
(`no_kaspi` | `check_failed`); with an invoice, `payment: {method:
"kaspi_invoice", phone, amount}`. `NO_KASPI` is no longer returned by the batch.

«❌ Жоқ» → bookings `cancelled` (reason `consent_declined`). No answer within
`CONSENT_TTL_SECONDS` → `cancelled` (`consent_expired`), client told (template
`booking_cancelled` if silent 24h+). Manager reschedules (date/time/field) now
notify the client (`booking_edited`).

Config: `OFFER_URL`, `OFFER_VERSION`, `CONSENT_TEXT_VERSION`,
`CONSENT_TTL_SECONDS` (1800), `KASPI_PHONE_MAX_RETRIES` (2), `LINK_PAYMENT_TTL_SECONDS` (7200),
`ADMIN_ALERT_PHONE`, `WA_TEMPLATE_LANG` (ru), `WA_TEMPLATE_<PURPOSE>` for each
template in `docs/whatsapp_templates.md`.
