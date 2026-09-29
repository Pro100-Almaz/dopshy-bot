# Payment system — overview

Two independent payment paths lead to the same booking-state transition
(`awaiting_payment → confirmed`). This doc is the map of both; for the full
detail on the online path see [`apipay.md`](apipay.md).

## 1. The two paths

**A. ApiPay online avans (primary, when configured)**
Client gets a Kaspi Pay push notification and pays in-app. Driven entirely
server-side via webhook. See `apipay.md` for the full flow, webhook contract,
reconciliation, and cancellation/re-issue rules.

**B. Manual PDF receipt (fallback / always available)**
Client pays to a company Kaspi/Halyk account off-platform and uploads a PDF
receipt via WhatsApp; the bot parses and validates it. Detailed in §3 below.

`config.APIPAY_ENABLED = bool(APIPAY_API_KEY and APIPAY_WEBHOOK_SECRET)`
(`config.py:184`) — when false, the system runs receipt-only.

Both paths write into the shared `payments` table (§4), and both are retired
if the other succeeds first: `booking_service.submit_payment_proof`
(`integrations/booking_service.py:266-278`) cancels any live ApiPay invoice
once a receipt is accepted, so a client can't pay twice for one slot.

## 2. Booking state machine (payment-relevant states)

```
draft → awaiting_payment → confirmed
                ↓
             unpaid / cancelled   (TTL expiry, ApiPay failure, manager action)
```

- `awaiting_payment` is entered via `booking_service.request_payment`, which
  reserves the slot with a TTL (`config.BOOKING_SESSION_TTL`, default 1200s /
  20 min).
- A scheduled job `_cancel_expired_bookings` (`app.py:88`) sweeps expired
  `awaiting_payment` rows, releasing the slot to `unpaid` and cancelling any
  still-open ApiPay invoice **before** releasing, so a client can't pay into a
  booking that no longer exists.

## 3. Path B — manual receipt upload

**Files:** `integrations/receipt_parser.py` (PDF → structured fields),
`integrations/payment_validation.py` (business validation),
`handlers/message_handler.py:_handle_payment_receipt` (entry point),
`integrations/booking_service.py:submit_payment_proof` / `reject_payment`.

### Flow

1. Client sends a WhatsApp document while they have an `awaiting_payment`
   booking (`booking_repo.get_awaiting_payment_booking`).
2. PDF downloaded, then `receipt_parser.parse_receipt()` extracts fields with
   no OCR — both banks produce text-based PDFs: `bank` (kaspi/halyk/unknown,
   detected via localized markers), `amount`, `bin`, `name`, `phone`, `date`,
   `ref` (receipt number, bank-specific regex). Both RU and KK label variants
   are matched position-independently (label and value can be on separate
   lines).
3. `payment_validation.validate_receipt(booking, pdf)` checks, in order:
   1. Bank recognized + amount parsed.
   2. A receipt reference (`ref`) was extracted — required, dedup depends on it.
   3. Recipient matches an active row in `payment_recipients` (by BIN, phone,
      or name substring) — `booking_service.get_payment_recipients()`.
   4. `amount >= config.PAYMENT_MIN` (10 000 ₸ flat minimum — note:
      `config.PAYMENT_MIN_FRACTION` also exists but the current check path
      enforces the flat `PAYMENT_MIN`, not a percentage of `price_total`).
   5. Receipt has a date, within `config.PAYMENT_RECEIPT_MAX_AGE_HOURS` (24h)
      of now, and not more than 10 minutes in the future (clock-skew allowance).
4. **Reject** → `booking_service.reject_payment()` logs a `payments` row with
   `status='rejected'`; booking **stays** `awaiting_payment` so the client can
   retry within the TTL.
5. **Accept** → `booking_service.submit_payment_proof()`:
   - Inserts a `payments` row (`method='bank_transfer'`, `status='accepted'`).
   - `transaction_ref` (the parsed receipt number) has a **UNIQUE** index → a
     replayed/reused receipt raises `psycopg2.errors.UniqueViolation`, caught
     and returned as `PAYMENT_DUPLICATE`. This is the anti-fraud mechanism for
     this path — a receipt without a parseable `ref` is rejected upstream
     specifically so it can never slip through the NULL-exempt partial unique
     index.
   - Booking → `confirmed` (both halves, for a day-crossing/transitive
     booking).
   - Any still-open ApiPay invoice for the booking is cancelled, so the
     client can't also confirm the Kaspi push after paying by receipt.

## 4. Shared `payments` table

Populated by both paths with a consistent shape: `booking_id, method
('apipay'|'bank_transfer'), bank, amount, transaction_ref, status
('accepted'|'rejected'), verified_by, verified_at, reject_reason`. This is
what lets manager reporting treat ApiPay pushes and manual Kaspi/Halyk
transfers uniformly.

## 5. Pricing (separate from the avans)

`handlers/payment/pricing.py` computes the **full booking price** (not the
avans) — used for display and as the receipt-validation floor context:

- `calculate_booking_price()` — time-of-day tiered pricing
  (`morning_day`/`evening`/`late_night`/`after_midnight`), per format
  (`5x5`/`6x6`), sourced from `pricing_repo`. On Mon–Fri, 12:00–16:00 and
  18:30–20:00 are charged at `weekday_special` instead; weekends use the
  ordinary periods (there is no weekend/holiday rate).
- `calculate_full_booking_price()` — handles midnight-crossing bookings by
  summing two segments.
- The avans (`PAYMENT_MIN` / `APIPAY_AVANS_PER_BOOKING`, both 10 000 ₸ by
  default) is a **flat deposit, independent of this computed total** — not a
  percentage of price, just a fixed prepayment amount.

## 6. Key invariants worth knowing before touching this code

- **Commit-then-call.** Bookings/invoice rows are always committed to
  Postgres before any outbound call to ApiPay. Never reorder this — it's what
  makes the outbox/retry model correct. See `apipay.md` §5.
- **No invoice → no booking.** A failed ApiPay send always rolls back the
  reservation synchronously in the request path; only the background sweeper
  retries silently, because in that case the client was never told anything
  yet.
- **One invoice ↔ one batch, no partial amounts.** ApiPay can't reprice a
  live invoice, so any change to what a batch invoice covers means cancel +
  re-issue, never a mutation. See `apipay.md` §5a.
- **Idempotency is transaction-scoped.** Both webhook claim+apply and
  manual-receipt insert+confirm happen in one DB transaction each,
  specifically to avoid a "claimed but not applied" window that would strand
  a paid booking.
- **`config.PAYMENT_MIN_FRACTION` exists but isn't wired into
  `payment_validation.py`'s active check** — it only reads the flat
  `config.PAYMENT_MIN`. Worth confirming intent before relying on it being a
  percentage-of-price check.
