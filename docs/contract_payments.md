# Contract payment plans ("subscriptions")

A contract's `price` can be collected in **installments**, each asked for on its
own due date through ApiPay (Kaspi) — or, for a number that is not in Kaspi, as a
payment link on WhatsApp. Code: `integrations/contract_billing.py`; schema:
`migrations/056_contract_payment_plans.sql`; tests: `tests/test_contract_billing.py`.

Contracts created without `payment_plan` behave exactly as before.

---

## 1. Why not ApiPay's `/subscriptions`

ApiPay subscriptions bill **one fixed amount on a fixed calendar** and only to a
Kaspi number. We need:

- a **per-booking** mode, where the due date follows each booking and the amount
  follows what is left to pay (bookings can be added or cancelled mid-contract),
- a **WhatsApp link** path for numbers without Kaspi,
- manager overrides on individual installments.

So we schedule installments ourselves and raise each one as a **regular ApiPay
invoice** (`POST /invoices`). That reuses the whole existing pipeline from
`docs/apipay.md` — outbox + retry, signed webhook, reconciliation poller,
refund flagging. An installment invoice is an `apipay_invoices` row with
`contract_installment_id` set and `booking_ids = '{}'`, so the booking-avans
logic (confirm on paid, release on failed, re-issue on cancel) never touches it.

---

## 2. Modes

### static — fixed number of payments

| Field | Meaning |
|---|---|
| `frequency` | `weekly` · `biweekly` · `monthly` |
| `installments` | how many; if omitted, one per period from `first_due_date` until the contract's `end_date` |
| `first_due_date` | defaults to the contract's `start_date` |
| `due_dates` | alternative to `frequency`: explicit list of `YYYY-MM-DD` |
| `amounts` | optional, one per installment, must add up to `price` exactly |

Without `amounts` the price is split evenly in whole tenge, the remainder on the
last installment (100 000 / 3 → 33 333 · 33 333 · 33 334). Each installment is
sent on its due date at `CONTRACT_BILLING_TIME` (default 13:00 Almaty).

### dynamic — one payment per booking

One installment per booked **slot** (a day-crossing booking is one slot), due
when that booking **ends**. The amount is decided when it is issued:

```
            price − paid − invoiced but unpaid − amounts fixed by a manager
amount  =  ──────────────────────────────────────────────────────────────────
                       installments not yet issued (incl. this one)
```

Rounded down to whole tenge; the last installment takes exactly what is left.

Example from the spec: price 200 000, 2 bookings → 100 000 after each.
Then a third booking is added after the first was paid:
(200 000 − 100 000) / 2 → 50 000 each for the remaining two.
A booking cancelled before it is billed takes its installment with it, and the
others grow to cover the price. A manager can fix any one share
(`PATCH …/installments/{id}` with `amount`), and the rest re-spread around it.

---

## 3. Channel: Kaspi invoice or WhatsApp link

Decided **every time an installment is sent**, by `POST /clients/check` on
`billing_phone` (falls back to the contract's `phone`). So a contract created
before ApiPay was configured, or for a client who joins Kaspi later, gets Kaspi
invoices from its next payment on. `contracts.payment_channel` shows the channel
of the latest send (and, before the first send, the result of the check made at
creation):

| Result at send time | Channel | How it is settled |
|---|---|---|
| number is in Kaspi | `kaspi_invoice` — ApiPay push to the Kaspi app | automatically, by webhook |
| number not in Kaspi | `whatsapp_link` — amount + `KASPI_PAYMENT_URL` on WhatsApp | manager: `mark-paid` |
| ApiPay not configured | `whatsapp_link` | manager: `mark-paid` |
| the check itself fails | nothing is sent; the installment stays `scheduled` and the next sweep (1 min) retries. "Send now" answers `502 PAYMENT_PROVIDER_ERROR` | — |

At creation the same check runs once so the manager sees the expected channel;
if it fails, nothing is created (`502`).

`payment_plan.channel: "whatsapp_link"` forces the link for good
(`contracts.payment_channel_forced`, migration 057) — Kaspi is never asked.
`"kaspi_invoice"` demands Kaspi at creation and fails with `NO_KASPI` for an
unregistered number.

"Send now" on an installment already sent as a WhatsApp link re-sends it as a
Kaspi invoice (same amount) when Kaspi is available now, and only re-sends the
link otherwise.

---

## 4. Lifecycle

```
installment: scheduled ──(due, scheduler every minute)──▶ issued ──(webhook paid)──▶ paid
                 ▲                                          │
                 └──── expired/cancelled/error, attempt < MAX (retry after RETRY_HOURS)
                                                            │
                                  attempts used up ────────▶ overdue ──(manager "send")──▶ issued
any unpaid ──(contract cancelled / booking cancelled / plan stopped)──▶ cancelled
```

- `CONTRACT_INVOICE_MAX_ATTEMPTS` (default 3) invoices per installment, re-sent
  `CONTRACT_INVOICE_RETRY_HOURS` (default 24) after one dies unpaid. The amount
  stays the same on a retry.
- Contract bookings stay `confirmed` whatever happens to payments. Payment state
  lives on the contract, not the bookings.
- `contracts.payment_status` rolls it all up: `none` · `scheduled` · `awaiting`
  (an invoice is out) · `overdue` (needs a manager) · `paid` · `stopped` (plan
  halted before the price was collected) · `cancelled`.
- Cancelling takes back the open invoice at ApiPay. If the client pays it anyway,
  nothing is counted and the invoice is flagged `paid_after_cancellation`, the
  same `ТРЕБУЕТСЯ РУЧНОЙ ВОЗВРАТ` log line as the avans flow. Refunds stay manual.
- A price change re-spreads the static schedule. Dynamic shares already follow
  the price.
- A manual payment of a different amount re-spreads the rest of a static
  schedule; paying the whole price by hand cancels the remaining installments.

---

## 5. API (all under `/api/manager`, same auth as the rest)

### Create — `POST /contracts` (existing endpoint, new optional field)

```json
{
  "customer_name": "ТОО Ромашка", "phone": "+7 700 123 45 67",
  "start_date": "2026-10-01", "end_date": "2026-12-31", "price": 450000,
  "slots": [{"field": 1, "date": "2026-10-01", "time_start": "19:00", "time_end": "21:00",
             "repeat_mode": "weekly", "repeat_until": "2026-12-31"}],
  "payment_plan": {
    "mode": "static",                 // or "dynamic"
    "billing_phone": "87001234567",   // optional, defaults to phone
    "frequency": "monthly",           // static
    "installments": 3,                // static, optional
    "first_due_date": "2026-10-01",   // static, optional
    "due_dates": ["…"],               // static, instead of frequency
    "amounts": [150000, 150000, 150000], // static, optional
    "channel": "whatsapp_link"        // optional override
  }
}
```

Response `data` gains `payment_plan` (same shape as `GET …/payments`).
Errors: `400 INVALID_PLAN` / `NO_KASPI`, `409 SLOT_TAKEN` (now with every
conflicting occurrence in `conflicts`), `502 PAYMENT_PROVIDER_ERROR`.

### Modal helpers (write nothing)

| Endpoint | Body | Answer |
|---|---|---|
| `POST /contracts/check-slots` | `{slots, start_date?, end_date?}` | `{free, conflicts: [{field, date, time_start, time_end}], occurrences}` |
| `POST /contracts/payment-plan/preview` | `{price, start_date, end_date, phone, payment_plan, slots?}` | `{mode, installments: [{seq, due_at?, amount}], installments_count}` |

`check-slots` backs the "check" button in step 4. The alternative ("already
display the booked slots") can keep using `GET /bookings/range/{start}/{end}`.

### Managing a plan

| Endpoint | Does |
|---|---|
| `GET /contracts/{id}/payments` | plan, `summary` {price, paid, invoiced_unpaid, left_to_pay, next_due_at, per_booking_estimate}, `installments[]` |
| `PUT /contracts/{id}/payment-plan` | set / replace. Paid and already-invoiced installments are kept; the new plan covers `price − paid − invoiced` |
| `DELETE /contracts/{id}/payment-plan` | stop billing: unpaid installments cancelled, open invoices taken back |
| `PATCH /contracts/{id}/installments/{iid}` | `{amount?, due_date?}` on a not-yet-issued installment. `amount: null` returns it to automatic |
| `POST /contracts/{id}/installments/{iid}/mark-paid` | `{amount?, note?}`: money received outside ApiPay |
| `POST /contracts/{id}/installments/{iid}/send` | issue now (scheduled / overdue), or resend the WhatsApp link |
| `POST /contracts/{id}/bookings/batch` | (existing) adds bookings; a dynamic plan gets installments for them (`installments_added`) |

Installment object: `id, seq, booking_id, due_at, amount, amount_locked, status,
attempts, channel, invoice_id, invoice_status, issued_at, paid_at, paid_amount,
paid_via, last_error`.

---

## 6. Configuration

```
CONTRACT_BILLING_TIME=13:00          # static installments are sent at this local time
CONTRACT_INVOICE_RETRY_HOURS=24      # wait before re-sending an unpaid installment
CONTRACT_INVOICE_MAX_ATTEMPTS=3      # invoices per installment before 'overdue'
KASPI_PAYMENT_URL=…                  # the link sent to numbers without Kaspi (existing)
```

The scheduler job `_issue_contract_installments` (every minute, `app.py`) does
the issuing. It runs in every gunicorn worker and is safe to do so: each
installment is taken `FOR UPDATE SKIP LOCKED`, with the contract row locked
while its amount is worked out.

---

## 7. Open questions (decided provisionally — change if needed)

1. **When is a dynamic installment due?** When the booking *ends* ("after each
   booking the customer has to pay"). Billing *before* each booking would be a
   one-line change in `_insert_booking_installments` (`end_at` → `start_at`).
2. **Contracts with an unknown end date** ("Period + Unknown state?"):
   `contracts.end_date` is still required. A static plan can already run without
   a period (`installments` + `frequency`), but the contract row itself needs a
   date. Making `end_date` nullable touches the slot-range checks and sheet sync,
   so it was left for a separate change.
3. **Paying by link is confirmed by hand.** A static Kaspi link can't tell us who
   paid. ApiPay's per-deal payment link (`POST /invoices/qr` with `static: true`)
   would settle by webhook, but it needs a paid ApiPay plan, and the payer still
   needs Kaspi.
4. **No `payments` rows for installments.** The `payments` table is per booking.
   Installment money is recorded on `contract_installments`
   (`paid_amount`, `paid_via`) and on `apipay_invoices`.
