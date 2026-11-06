# Offer consent — website contract

Offer URL: `https://www.dopsy.kz/offer` (`OFFER_URL`).

No Kaspi (ApiPay) invoice is raised until the client has agreed to the offer
(the avans is refundable within 48 hours of payment). On the website the agreement is a checkbox; the
backend stores it on every booking as proof (`consent_at`, `consent_channel =
"site"`, `offer_version`, `consent_text_version`) and refuses to invoice without it.

## UI

- Checkbox before the submit button: **«Офертамен танысып, келісемін. Авансты
  төлегеннен кейін 48 сағат ішінде қайтаруға болады»** (ru: «С офертой
  ознакомлен(а) и согласен(на). Аванс можно вернуть в течение 48 часов после
  оплаты»), the word «Оферта» linking to the offer URL.
- Submit stays **disabled** until it is ticked (the backend rejects it anyway).
- A separate **Kaspi number** field, prefilled with the client's phone. The
  invoice goes there.
- The form opened from the bot's prefill link (`/bot/prefill`) shows the same
  checkbox and field.

## Request — `POST /api/manager/bookings/batch` (server side, `X-API-Key`)

```json
{
  "channel": "site",
  "slots": [{ "field": 1, "date": "2026-11-05", "time_start": "10:00", "time_end": "11:00" }],
  "customer": "Ерсултан",
  "phone": "77011234567",
  "kaspi_phone": "77079998877",
  "consent": {
    "accepted": true,
    "at": "2026-11-01T09:15:00+05:00",
    "offer_version": "2026-10-31",
    "text_version": "site-v1"
  }
}
```

| Field | Rule |
|---|---|
| `channel` | Always `"site"`. Marks the request as the website's, so a missing `consent` is an error instead of falling into the manager flow. |
| `consent.accepted` | Must be exactly `true`, else `400 CONSENT_REQUIRED` and nothing is created. |
| `consent.offer_version` / `text_version` | Versions of the offer and checkbox text shown. Stored on the booking. Default to the server's `OFFER_VERSION` / `CONSENT_TEXT_VERSION`. |
| `consent.at` | Informational; the stored `consent_at` is the server time the request was accepted. |
| `kaspi_phone` | Number the invoice is sent to; default `phone`. Remembered as the client's Kaspi number for next time. |

## Responses

| Status | `code` | What the UI does |
|---|---|---|
| 200 | — | `payment.method = "kaspi_invoice"` → tell the client to confirm the payment in the Kaspi app. `payment.method = "link"` → the payment number has no Kaspi (or the check failed): show the `payment.url` button and "send the receipt to WhatsApp, an admin confirms". The client also gets this on WhatsApp. |
| 400 | `CONSENT_REQUIRED` | Checkbox not ticked. |
| 400 | `INVALID` | Bad body / phone format (`message` in ru). |
| 409 | `SLOT_TAKEN` | `conflicts[]` lists the clashes. Nothing created. |
| 502 | `PAYMENT_PROVIDER_ERROR` | Invoice could not be sent; bookings were released. Safe to retry. |

## Admin card

`GET /api/manager/bookings/{id}` returns `consent_at`, `consent_channel`,
`kaspi_phone`, `offer_version`, `consent_text_version`, `apipay_invoice_id`,
`consent_step`. Show «Офертамен келісті: [date, time]» from `consent_at`, and
flag `payment_method = "link"` (confirm by receipt, or bill a corrected number via
`POST /api/manager/bookings/{id}/invoice {kaspi_phone}`). New state
`awaiting_consent` («ОЖИДАЕТ СОГЛАСИЯ») needs a label in the UI.

## Admin-created bookings (2026-11-06)

`POST /api/manager/bookings` and `/batch` without `consent`/`channel: "site"`
(the admin panel) no longer ask the client to agree on WhatsApp. The admin books
on the client's behalf, so the offer counts as accepted (`consent_channel =
"admin"`, migration 068) and the response is the same as the website's:
`payment.method = "kaspi_invoice"` or `"link"`. The invoice goes to
`kaspi_phone`, else the client's last Kaspi number, else `phone`. If the invoice
cannot be sent the bookings are kept and the client pays by the link (website:
502, bookings released). The response no longer carries `consent`.

Website and admin clients get one WhatsApp message — the booking, how to pay,
and «Создавая бронь, вы приняли условия публичной оферты: OFFER_URL. Если есть
вопросы — позвоните администратору». The WhatsApp bot flow is unchanged.

### 24-hour holds

`POST /api/manager/bookings/{id}/request-payment` (`{phone?, kaspi_phone?}`)
no longer starts a WhatsApp dialog: it bills the hold right away like an admin
booking (consent channel `admin`, Kaspi invoice or payment link) and answers
with the same `payment` object. Only a hold still in `awaiting_payment` can be
billed, else `409 NOT_A_HOLD`. A hold can also be edited like any booking
(status, `paid_cash`, …) — the old `409 CONSENT_REQUIRED` guard is gone.
Setting any booking to `confirmed` by hand cancels its still-open Kaspi invoice.
