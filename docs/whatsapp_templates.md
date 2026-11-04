# WhatsApp templates (Meta) — what to submit

WhatsApp delivers free-form messages only to a number that wrote to us in the
last 24 hours. Sending a template does **not** open that window; the client's
reply does — including a tap on a template's quick-reply button. So:

* outside the window every bot-initiated message must be a template;
* once the client taps a button (or writes), the rest of the conversation
  (Kaspi number, payment link, …) is free-form for 24h.

The bot picks automatically: inside the window it sends the richer free-form
text, outside it the template with the same purpose (`integrations/client_notify.py`).
Each template's name goes into an env var; an empty var means "not approved
yet" and the free-form text is attempted instead.

Category for all: **UTILITY**. Every template is approved in both `ru` and
`kk` under the same name; the bot sends the client's language when it knows it
(the bot conversation, `lang` on a manager booking, an invoice's language) and
`WA_TEMPLATE_LANG` (default `ru`) otherwise. Variables are built in Russian and
translated for the `kk` version (months, «поле N», reasons, the payment line —
`client_notify.localize_param`).

Approved names (WABA 4397420440331456, sent through YCloud from bot 1):

| Purpose | Template name |
|---|---|
| booking_confirm_request | `booking_confirm_request` |
| booking_created | `template_utility_20261031171652` |
| booking_edited | `booking_updated_template` |
| booking_confirmed | `template_utility_20261031173838` |
| booking_cancelled | `template_utility_20261031174248` |
| payment_link | `payment_link` |
| admin_alert | `template_utility_20261031183242` | Variables must be single-line; the bot flattens lists
with `; `.

| # | Env var | When it is sent | Variables | Buttons |
|---|---|---|---|---|
| 1 | `WA_TEMPLATE_BOOKING_CONFIRM_REQUEST` | Admin created a booking → the client confirms or cancels **the booking**. On «Подтверждаю» the bot sends the offer-consent question as a normal message (the tap opened the 24h window). No answer in 30 min → booking cancelled (template 5). | `{{1}}` booking | Quick reply ×2 |
| 2 | `WA_TEMPLATE_BOOKING_CREATED` | Client booked on the website and the Kaspi invoice went out. | `{{1}}` booking, `{{2}}` payment line | — |
| 3 | `WA_TEMPLATE_BOOKING_EDITED` | Admin moved the booking (date, time or field). | `{{1}}` the booking as it is now | — |
| 4 | `WA_TEMPLATE_BOOKING_CONFIRMED` | Avans paid (ApiPay) or admin set "confirmed". | `{{1}}` booking | — |
| 5 | `WA_TEMPLATE_BOOKING_CANCELLED` | Any cancellation: no answer to template 1, not paid in time, invoice expired/failed, cancelled by admin. | `{{1}}` booking, `{{2}}` reason | — |
| 6 | `WA_TEMPLATE_PAYMENT_LINK` | Website payment number without Kaspi → pay by link (in the bot dialog the client is in the chat, so free-form is used). Also contract instalments without Kaspi. | `{{1}}` booking, `{{2}}` amount, `{{3}}` link | — |
| 7 | `WA_TEMPLATE_ADMIN_ALERT` | To `ADMIN_ALERT_PHONE`: no Kaspi number found for a client (or ApiPay could not check it) → the client got the payment link, the admin must contact them and confirm the payment by receipt. | `{{1}}` reason, `{{2}}` client, `{{3}}` booking | — |

There is no separate "draft" template: the draft is template 1 (the client's
confirmation is what turns the admin's draft into a real booking), and for bot
bookings the client is already in the chat.

Meta rules that matter here: a variable may not be the first or last thing in
the body, and a short body with many variables is rejected — keep the fixed
text long relative to the number of variables.

## Suggested texts (RU — translate to KK with the same variables)

**1. booking_confirm_request**
```
Здравствуйте! Администратор Dopsy Arena оформил для вас бронь: {{1}}.

Пожалуйста, подтвердите бронь, нажав кнопку ниже. Если бронь вам не нужна, нажмите «Отменить» — слот освободится для других игроков. Без ответа бронь автоматически снимется через 30 минут.
```
Quick replies (as approved): `Подтвердить` / `Отменить` (KK: `Растау` / `Болдырмау`).
The bot reads the reply **text**, so keep these words. This only confirms the
booking; the offer consent («✅ Согласен / ❌ Нет» with the offer link and the
avans) is the next message, sent free-form because the tap opened the window.
KK example:
```
Сәлеметсіз бе! Dopsy Arena администраторы сізге брондау жасады: {{1}}.

Төмендегі батырманы басып, брондауды растаңыз. Брондау қажет болмаса, «Бас тарту» батырмасын басыңыз — слот басқа ойыншыларға босатылады. Жауап болмаса, брондау 30 минуттан кейін автоматты түрде жойылады.
```

**2. booking_created**
```
Бронь в Dopsy Arena создана: {{1}}.
{{2}}. Аванс не возвращается, без оплаты бронь отменится через 20 минут.
```
`{{2}}` e.g. «счёт на аванс 10 000₸ отправлен в Kaspi на номер +7 707 111 22 33».

**3. booking_edited**
```
Ваша бронь в Dopsy Arena изменена.
Было: {{1}}
Стало: {{2}}
Если что-то не так — напишите нам.
```

**4. booking_confirmed**
```
Бронь в Dopsy Arena подтверждена: {{1}}. До встречи на поле! ⚽
```

**5. booking_cancelled**
```
Бронь в Dopsy Arena снята: {{1}}.
Причина: {{2}}.
Слот снова свободен — напишите нам, если хотите забронировать заново.
```
Reasons the bot sends: «подтверждение не получено», «оплата не поступила
вовремя», «истёк срок оплаты счёта», «оплата не прошла», «бронь отменена
администратором», «серия броней отменена администратором», «оплата не поступила».

**6. payment_link**
```
Счёт в Kaspi выставить не удалось, поэтому оплатите аванс по ссылке.
Бронь: {{1}}
Сумма: {{2}}
Ссылка: {{3}}
После оплаты отправьте чек в этот чат — администратор подтвердит бронь.
```

**7. admin_alert** (3 variables, long fixed text)
```
⚠️ Dopsy Arena — нужна помощь администратора по брони.

Что случилось: {{1}}.
Клиент: {{2}}.
Бронь: {{3}}.

Счёт Kaspi клиенту не выставлен, поэтому ему отправлена ссылка на оплату аванса. Пожалуйста, свяжитесь с клиентом, проверьте чек об оплате и подтвердите бронь в админ-панели. Если оплата не будет подтверждена, бронь автоматически снимется через 2 часа.
```
Example values:
`{{1}}` «Kaspi не найден ни на одном из номеров клиента (спрошено 2 раз)» /
«на номере для оплаты нет Kaspi» / «проверка номера в ApiPay не удалась»,
`{{2}}` «Ерсултан, +7 700 123 45 67, номер для оплаты +7 707 111 22 33»,
`{{3}}` «📅 5 ноября, 18:00–19:00 · поле 1 (#123)».
The "2 часа" is `LINK_PAYMENT_TTL_SECONDS`; change the text if you change it.

## Two numbers per booking

* **Contact number** — `phone`: the client's WhatsApp; every message goes here.
* **Payment number** — `kaspi_phone`: where the Kaspi invoice goes. Defaults to
  the contact number. Website: the "Kaspi number" field. Admin: the
  "Номер для оплаты (Kaspi)" field. Bot: the number billed last time (asked
  "same number?") or the WhatsApp number.

If the payment number has no Kaspi, the bot asks for another number (at most
`KASPI_PHONE_MAX_RETRIES` = 2 times; a badly formatted number is re-asked and not
counted). If none has Kaspi — or ApiPay cannot check it, or it is the website's
payment number — the booking is **not** refused: the client is told
«Администратор сізбен байланысады» and it waits in `awaiting_payment` with `payment_method = link` for
`LINK_PAYMENT_TTL_SECONDS` (2h), the client gets the payment link (template 6
outside the window), and the admin gets template 7. The admin confirms the
booking once the receipt arrives, or bills a corrected number with
`POST /api/manager/bookings/{id}/invoice {kaspi_phone}`.
