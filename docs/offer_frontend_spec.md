# Offer consent — website contract

Offer URL: `https://www.dopsy.kz/offer` (`OFFER_URL`).

No Kaspi (ApiPay) invoice is raised until the client has agreed to the offer
(the avans is non-refundable). On the website the agreement is a checkbox; the
backend stores it on every booking as proof (`consent_at`, `consent_channel =
"site"`, `offer_version`, `consent_text_version`) and refuses to invoice without it.

## UI

- Checkbox before the submit button: **«Офертамен танысып, келісемін. Аванс
  қайтарылмайды»** (ru: «С офертой ознакомлен(а) и согласен(на). Аванс не
  возвращается»), the word «Оферта» linking to the offer URL.
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
