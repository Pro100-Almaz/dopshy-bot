# ApiPay.kz — online avans via Kaspi Pay

Integration guide followed: <https://apipay.kz/for-ai>

ApiPay is an independent service that collects payments through Kaspi Pay. We
use it for the **avans (prepayment)** on bookings made from the manager UI and
from the WhatsApp bot alike — one avans of `APIPAY_AVANS_PER_BOOKING` per slot,
pushed to the client's Kaspi app.

---

## 1. What was added

| File | Role |
|---|---|
| `integrations/apipay_client.py` | HTTP only — check client, create/get/cancel invoice, phone normalisation, webhook signature |
| `integrations/apipay_service.py` | Booking-side logic — invoice hook, paid/failed transitions, TTL cancel |
| `integrations/repo/apipay_repo.py` | SQL only — the `apipay_invoices` table |
| `blueprints/apipay_webhook.py` | `POST /webhooks/apipay` |
| `migrations/037_apipay_invoices.sql` | `apipay_invoices` table |
| `migrations/038_apipay_kaspi_invoice_id.sql` | `kaspi_invoice_id` for DBs that applied 037 before it declared the column |
| `migrations/039_apipay_outbox.sql` | outbox columns (`send_attempts`, `last_send_error`) + WhatsApp notify context |
| `tests/test_apipay.py` | 75 tests; ApiPay is stubbed, nothing hits the network |

`config.APIPAY_ENABLED` is `True` only when **both** `APIPAY_API_KEY` and
`APIPAY_WEBHOOK_SECRET` are set. Otherwise the webhook is not registered, no
invoice is created, and `bookings/batch` behaves exactly as before.

---

## 2. Environment variables

Put these in `.env` (see `.env.example`). Both secrets are **server-side only** —
never expose them to the browser or the Apps Script manager UI.

```
APIPAY_API_KEY=
APIPAY_WEBHOOK_SECRET=
APIPAY_BASE_URL=https://api.apipay.kz/api/v1
APIPAY_TIMEOUT=10
APIPAY_AVANS_PER_BOOKING=10000
APIPAY_CHECK_CLIENT=1
```

`APIPAY_CHECK_CLIENT` (default on) is the Kaspi registration check of §4a. Turn
it off only if `POST /clients/check` itself starts misbehaving — with it off the
old behaviour returns: an unregistered number is found out by webhook, minutes
after the request that could have reported it.

---

## 3. Manual steps in the ApiPay dashboard

These cannot be automated — the two secrets are each shown **once**, at creation.

1. **API key** — Настройки → вкладка «Подключение» → «Создать новый ключ».
   Copy it into `APIPAY_API_KEY`.
2. **Webhook URL** — same key card → «Изменить» → field «Адрес для уведомлений»:
   `https://<your-domain>/webhooks/apipay`
   Production requires a real HTTPS domain; ngrok/IP is rejected
   (`422 webhook_url_requires_domain`).
3. **Webhook secret** — same key card → «Создать подпись».
   Copy it into `APIPAY_WEBHOOK_SECRET`, then restart the app.
4. **Test delivery** — same key card → «Проверить уведомления». It sends a
   `webhook.test` event; the log line `[APIPAY] Тестовый вебхук получен` means
   the signature verified.

### Before going live

- Cashier connected (<https://apipay.kz/connect-cashier>) — otherwise real
  invoices fail with `400 kaspi_session_not_configured`.
- Business profile filled (<https://apipay.kz/business-profile>) — until it is
  approved you get **1 real invoice per day** (`429 kyc_daily_limit_reached`).
- Dashboard toggled ТЕСТОВЫЙ → РАБОЧИЙ.

---

## 4. How the money is calculated

On `POST /api/manager/bookings/batch`:

```
invoice amount = APIPAY_AVANS_PER_BOOKING × (number of NON-repeating slots)
```

- 2 one-off slots → **20 000 ₸**, a single invoice.
- A slot with `repeat_mode = daily|weekly|monthly` is **not charged**, however
  many occurrences it expands into.
- A batch of only repeating slots → **no invoice at all**; the bookings are
  created in `awaiting_payment` and handled by the manual receipt flow.
- A day-crossing slot (e.g. 23:00→01:00) is stored as two linked rows but is
  one booking, so it is charged once.

Each charged booking also gets `paid_avans = 10000` so the manager sheet shows
the avans per booking.

The invoice is pushed to the `phone` field of the request body, normalised to
ApiPay's strict `8XXXXXXXXXX` form (`+7…`, `7…`, spaces and brackets accepted).
If ApiPay is enabled and there is something to charge, `phone` is required.

---

## 4a. Is the number in Kaspi? (`POST /clients/check`)

Before **anything** is written, both entry points ask ApiPay whether the phone is
registered in Kaspi — `apipay_service.ensure_kaspi_client(phone)`.

```
POST /api/v1/clients/check   {"phone": "87001234567"}
→ {"phone": "87001234567", "has_kaspi": true, "client_name": "Иван И."}
```

Why it has to happen first — an invoice raised for a number Kaspi does not know
is **not** rejected by `POST /invoices`:

- it is accepted, and the failure arrives later as a `status=error` **webhook** —
  after the request that created it has answered, so there is no way left to tell
  the manager (they saw `200 OK`) or the client (they were told to open Kaspi);
- the dead invoice still counts against the account's **daily creation quota**;
- the slot is reserved by then, so the failure costs a booking that has to be
  cancelled, instead of a reply.

With the check, all three become one answer in the same call:

| Entry point | Not registered | Check itself failed (ApiPay down) |
|---|---|---|
| `bookings/batch` | `400 NO_KASPI`, nothing created | `502 PAYMENT_PROVIDER_ERROR`, nothing created |
| WhatsApp bot | `apipay_no_kaspi` reply, draft stays a draft | `apipay_failed` reply, draft stays a draft |

Both failures are fatal on purpose. Nothing has been reserved at that point, so
refusing costs the client nothing — while continuing would only move the same
failure to `send_invoice`, which has to take a committed booking back.

The check is skipped where there is nothing to charge (a batch of only repeating
slots) and on re-sends — the sweeper and the re-issue after a partial cancel work
from a number that was already verified when the booking was made.

---

## 5. Flow

Both entry points — the manager batch and the WhatsApp bot — share one shape:
**write, commit, then send.** The ApiPay call is the only step that can succeed
on their side while failing on ours, so nothing that must survive it is left
uncommitted.

```
POST /api/manager/bookings/batch          bot: draft confirmed in WhatsApp
  │                                          │
  └─ POST /clients/check ────── has_kaspi? ──┘   no → refused here, §4a
  │                                          │      nothing written, nothing spent
  ├─ bookings inserted (awaiting_payment) ─┐  ├─ booking → awaiting_payment  ─┐
  └─ apipay_invoices row: status=created  ─┘  └─ apipay_invoices row (bot)   ─┘
       one DB transaction, NO network            one DB transaction
        │                                          │
       COMMIT ◄─── the point of no return ────────COMMIT
        │                                          │
        └─ send_invoice() → POST /invoices ────────┘
             ├─ ok    → status=processing, invoice_id stored
             │          → client confirms in their Kaspi app
             └─ error → manager: row STAYS queued, retried by the sweeper
                                 200 + invoice.send_pending=true
                        bot:     falls back to the Kaspi link + PDF receipt,
                                 and RETIRES the row — the client has been given
                                 another way to pay, so a retry an hour later
                                 would be a second ask for the same money

POST /webhooks/apipay
  ├─ status=paid                     → covered bookings → confirmed
  │                                    + one `payments` row each (method='apipay')
  │                                    + bot invoices: client told on WhatsApp
  ├─ status=cancelled|expired|error  → covered bookings → unpaid (slot freed)
  └─ event=webhook.test              → 200, no side effects

Outbox sweeper (every 1 min, app.py)
  └─ re-sends rows still `created` with invoice_id IS NULL, reusing their
     external_order_id; drops those whose bookings no longer await payment,
     and re-cuts the amount to the slots that survived the wait.
     Bot rows reach it only when the client was never answered at all (a
     worker died between COMMIT and send) — there the retry is the only
     payment request they will ever get

Reconciliation poller (every 2 min, app.py)
  └─ GET /invoices/{id} for sent invoices quiet for 5+ min, and feeds the
     answer through the SAME claim-and-apply the webhook uses — the safety
     net for a delivery that never arrived

TTL sweeper (every 5 min, app.py)
  └─ before releasing an expired slot, cancels its still-open invoice AND
     retires any queued-but-unsent row, so the client can neither pay for a
     booking they lost nor be billed for one later
```

### Why the commit sits in the middle

Creating the invoice inside the booking transaction looked safer — a failure
left no half-written state. But a **lost response** (timeout after ApiPay had
already created the invoice) rolled our row back while their invoice lived on.
The client could pay it, and the webhook then arrived for an invoice we had no
record of: money taken, no booking, one WARNING line in the log.

Committing first inverts the failure: the worst case is now an invoice row with
`invoice_id IS NULL` that the sweeper retries, and any webhook that arrives
resolves against `external_order_id` regardless.

In a **mixed** batch, payment confirms only the bookings the invoice covered —
repeating bookings stay `awaiting_payment`.

---

## 5a. Cancelling an unpaid booking

Hooked into `postgres.cancel_booking_trial` and `booking_service.cancel_all_bookings`,
so **every** cancellation path is covered at once — manager `DELETE`s, the WhatsApp
cancel intent, the booking-session flows and the TTL sweeper.

A batch invoice covers several bookings and ApiPay cannot lower the amount of a
live invoice. So cancelling any covered booking:

1. **cancels the whole invoice** — never risk charging for a slot that is gone;
2. **re-issues** a fresh invoice for whatever is still `awaiting_payment`, at
   `10 000 ₸ × surviving slots`. The client gets a new Kaspi push.

```
invoice #4201 = 20 000 ₸ (A + B)      manager cancels A
  → #4201 cancelled
  → #4202 = 10 000 ₸ (B)              ← new push to the client
```

Guard rails:

- If ApiPay **refuses the cancel** (typically: already paid), nothing is
  re-issued — otherwise the client could be charged twice. The webhook stays
  the source of truth.
- If the cancel succeeds but the **re-issue fails**, the old invoice is still
  cancelled — no money can be taken wrongly. The survivors just have no online
  payment path and will expire on the TTL. Logged at ERROR.
- The whole hook never raises: a booking cancellation must succeed even with
  ApiPay unreachable.
- The **TTL sweeper deliberately bypasses the re-issue** (it bulk-cancels
  up front): the whole batch is expiring anyway, so re-issuing would send the
  client a pointless payment request microseconds before it, too, expires.

### Webhook payload

```json
{
  "event": "invoice.status_changed",
  "invoice": {
    "id": 42, "external_order_id": "batch-17-a1b2c3d4",
    "amount": "20000.00", "status": "paid",
    "description": "Аванс за бронь (2 шт.)",
    "kaspi_invoice_id": "13234689513",
    "client_name": "Иван Иванов", "client_phone": "87071234567",
    "paid_at": "2025-12-25T14:35:00Z"
  },
  "timestamp": "2025-12-25T14:35:01Z"
}
```

We read `invoice.id`, `invoice.status`, `invoice.paid_at`, `invoice.kaspi_invoice_id`
and (when present) `error_code` / `error_message`. The rest is deliberately
ignored — `external_order_id`, `client_name`, `client_phone`, `description` and
`timestamp` duplicate what the booking already holds. **`amount` from the payload
is never used for money**: the split written to `payments` comes from the amount
we recorded when raising the invoice.

`tests/test_apipay.py::test_real_apipay_payload_shape` asserts this exact body
end-to-end, so a field rename fails a test instead of silently no-op'ing payments.

### Paid-too-late (unavoidable race)

A client can be mid-payment in their Kaspi app while a manager cancels. If a
`paid` webhook arrives when no covered booking is `awaiting_payment` any more:

- **no booking is resurrected** (the slot may already be re-sold),
- the invoice is stamped `error_code = 'paid_after_cancellation'`,
- and an ERROR is logged:
  `[APIPAY] ⚠️ ТРЕБУЕТСЯ РУЧНОЙ ВОЗВРАТ: счёт … оплачен на …₸ …`

**Refunds are manual** — return the money from the ApiPay dashboard
(`POST /invoices/{id}/refund` is deliberately not wired up). Worth alerting on
that log line.

---

## 5b. What the manager sees in the history

Every ApiPay-driven change writes a `booking_history` row with
`source = 'bot:ApiPay'` — the same "a machine did this" shape as the WhatsApp
bot's `chatbot:Бот`, so `GET /api/manager/history?source=bot:ApiPay` returns
exactly the online-payment trail. Descriptions come from
`integrations/repo/history_descriptions.json`:

| Key | When | Rendered as |
| --- | --- | --- |
| `apipay_paid` | `paid` webhook (or the reconciliation poller) confirms the booking | `Оплата аванса 10000тг через Kaspi (ApiPay), счёт №4201.` |
| `apipay_cancelled` | the invoice is killed — `cancelled`/`error` from ApiPay, a manager cancelling the booking, a receipt paid it instead | `Счёт ApiPay №4201 отменён. Причина: бронь отменена менеджером.` |
| `apipay_ttl` | the reservation window ran out — `expired` from ApiPay, or our own TTL sweeper cancelling before it releases the slot | `Время на оплату истекло — счёт ApiPay №4201 закрыт, бронь освобождена.` |

Each of these sits **alongside** the usual `status_change` row (also
`bot:ApiPay`), which names the booking transition itself
(`awaiting_payment` → `confirmed` / `unpaid`). Queued-but-unsent rows that are
retired before ApiPay ever saw them write nothing: the client was never asked
for money, so there is no payment event to report.

The `[REASON]` token is rendered in Russian from a small map in
`apipay_service._STATUS_REASONS_RUSSIAN` / `_CANCEL_REASONS_RUSSIAN`; an unmapped reason falls through to its raw
slug rather than being hidden.

---

## 6. Correctness notes

- **Signature.** HMAC-SHA256 over the **raw** request body, compared with
  `hmac.compare_digest`. `blueprints/apipay_webhook.py` reads
  `request.get_data()` before any JSON parsing — re-serialising the parsed JSON
  would change the digest. A bad signature → `401`, nothing is touched.
- **Idempotency.** ApiPay retries up to 11 times over ~2 hours.
  `apipay_repo.claim_status` does a `SELECT … FOR UPDATE` and returns a row
  **only on a genuine status transition**, so redeliveries are no-ops
  (`{"status":"ok","duplicate":true}`). `payments.transaction_ref` is UNIQUE
  (`apipay:<invoice>:<booking>`) as a second line of defence.
- **The 5-second budget.** ApiPay expects a 2xx within 5 s. Booking state is
  written synchronously; the slow, best-effort Google Sheets sync runs in a
  background thread.
- **Webhook identity is `external_order_id`, not `invoice_id`.** We generate the
  former before calling ApiPay, so it is already committed when their webhook
  lands; `invoice_id` is only written after a successful send and can still be
  NULL at that moment. Keying on it alone would drop the transition, answer
  `200`, stop the retries, and strand a paid booking in `awaiting_payment`.
  `claim_status` resolves by `external_order_id` first and backfills
  `invoice_id` from whichever delivery is our first sighting of it.
- **Retries reuse the order id.** `external_order_id` is generated once, at
  queue time, and never regenerated — a re-send is the same order, not a
  second charge. (Re-issuing after a cancellation is a genuinely new invoice
  and does get a new id.)
- **Two payment paths, one winner.** The Kaspi link + PDF receipt still works.
  Confirming by receipt cancels any open or queued ApiPay invoice for that
  booking (`paid_by_receipt`), so a client cannot settle twice for one slot.
- **Unknown invoice** → still `200`, to stop a pointless retry storm.
- **The one failure worth finding out synchronously.** An unregistered number is
  the only ApiPay failure that can be detected *before* asking for money, so it
  is (§4a) — everything else (a declined payment, an expiry) genuinely can only
  arrive later, by webhook, and has the booking state machine behind it.
- **The push is not the only path.** Webhooks get lost — a deploy, DNS or TLS
  trouble, a rotated `APIPAY_WEBHOOK_SECRET` (every delivery then fails its
  signature check), or an outage outlasting their ~2 h of retries. The
  reconciliation poller pulls the status of any sent invoice that has been
  quiet for 5+ minutes and runs it through `apply_webhook_status`, so a poll
  and a delivery racing each other cannot both apply. It logs at **WARNING**:
  one is a hiccup, a run of them means the delivery path itself is broken.
- Only `awaiting_payment` bookings are transitioned, so a manager who already
  confirmed (or the sweeper that already released) a booking always wins.

---

## 7. Tests

```bash
POSTGRES_DSN=postgresql://user@host:5432/<disposable_db> \
  poetry run pytest tests/test_apipay.py
```

The test DB is **truncated between tests** — point it at a throwaway database,
never the live one.
