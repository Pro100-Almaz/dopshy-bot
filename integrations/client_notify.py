"""Status updates pushed to the client on WhatsApp.

Everything that changes a booking WITHOUT the client asking passes through here:
a manager cancelling or re-stating a booking in the sheet/UI, and an ApiPay
webhook settling (or killing) an invoice. The client is not in the room for any
of it, so silence means they find out by turning up to a slot that no longer
exists — or by never learning their payment failed.

Two rules the whole module is built around:

  * A notification NEVER breaks the thing it reports. Every public function
    swallows its exceptions: the booking transition is already committed, and a
    WhatsApp outage must not turn a successful cancel into a 500, nor make
    ApiPay retry a webhook we have already applied.
  * A notification never sits in front of a caller. `send_async` hands the HTTP
    call to a daemon thread, which is what the manager API uses — its response
    must not wait on Meta/YCloud, and the ApiPay webhook has a 5-second budget.

24-hour window: WhatsApp delivers free-form text only to a number that wrote
to us in the last 24 hours (sending a template does NOT open the window — the
client's reply does). Outside it, each message goes out as the Meta-approved
template with the same purpose (`TEMPLATES`, names in config.WA_TEMPLATES);
inside it, as the richer free-form text. `touch_inbound` records every inbound
message so the window is known.

Language: every notification goes out in Russian. `notify_lang` is still
accepted and carried by bot-created invoices, but the catalogue holds only RU,
so it currently changes nothing — re-adding a language means adding its key to
`MESSAGES`, not touching the senders.
"""

import logging
import re

import config
from integrations import test_context

logger = logging.getLogger(__name__)

# Field bookings all belong to bot 1, and it talks through YCloud — the same
# channel the TTL sweeper in app.py uses. Manager actions and manager-created
# invoices have no inbound message to infer a channel from, so this is it.
_DEFAULT_PROVIDER = "ycloud"


def _default_phone_number_id() -> str:
    return config.WHATSAPP_PHONE_NUMBER_ID_BOT_1


# ---------------------------------------------------------------------------
# Message catalogue
# ---------------------------------------------------------------------------
#
# One entry per event, `{token}` formatted by the caller. Russian only — the
# nesting by language is kept so a second language is a new key here rather than
# a change to `render` and its callers.

MESSAGES: dict[str, dict[str, str]] = {
    # ── Manager actions ───────────────────────────────────────────────────
    "manager_cancelled": {
        "ru": ("❌ Ваша бронь отменена.\n\n"
               "{slots}\n\n"
               "Если это ошибка — позвоните администратору"),
    },
    "manager_series_cancelled": {
        "ru": ("❌ Все повторяющиеся брони отменены.\n\n"
               "{slots}\n\n"
               "Если это ошибка — позвоните администратору"),
    },
    "manager_confirmed": {
        "ru": ("✅ Ваша бронь подтверждена!\n\n"
               "{slots}\n\n"
               "До встречи на поле! ⚽"),
    },
    "manager_unpaid": {
        "ru": ("⚠️ Бронь снята — оплата не поступила.\n\n"
               "{slots}\n\n"
               "Слот снова свободен. Хотите забронировать заново? Напишите нам!"),
    },

    # ── ApiPay ────────────────────────────────────────────────────────────
    "apipay_paid": {
        "ru": ("✅ Оплата получена — бронь подтверждена!\n\n"
               "💰 Аванс: {amount}\n"
               "⚠️ Возврат при неявке не производится.\n\n"
               "До встречи на поле! ⚽"),
    },
    "apipay_expired": {
        "ru": ("⏰ Срок оплаты счёта истёк — бронь снята.\n\n"
               "{slots}\n\n"
               "Слот снова свободен. Хотите забронировать заново? Напишите нам!"),
    },
    "apipay_failed": {
        "ru": ("❌ Оплата не прошла — бронь снята.\n\n"
               "{slots}\n\n"
               "Слот снова свободен. Попробуйте забронировать заново — "
               "или напишите нам, если нужна помощь."),
    },
    # ── Contract payment plans ────────────────────────────────────────────
    "contract_payment_link": {
        "ru": ("💳 Оплата по договору №{contract_id}\n\n"
               "{description}\n"
               "💰 К оплате: {amount}\n\n"
               "Оплатить: {link}\n\n"
               "После оплаты, пожалуйста, пришлите чек в этот чат."),
    },
    "contract_installment_paid": {
        "ru": ("✅ Оплата по договору №{contract_id} получена: {amount}.\n\n"
               "Спасибо!"),
    },
    # ── Booking lifecycle (also sent as templates outside the 24h window) ─
    "booking_edited": {
        "ru": ("✏️ Ваша бронь изменена.\n\n"
               "Было: {old}\n"
               "Стало: {new}\n\n"
               "Если что-то не так — напишите нам."),
    },
    "booking_created_invoice": {
        "ru": ("📋 Бронь создана!\n\n"
               "{slots}\n\n"
               "💳 Счёт на аванс {amount} отправлен в Kaspi на номер {kaspi_phone} — "
               "подтвердите оплату в приложении Kaspi.\n"
               "⚠️ Аванс не возвращается. Без оплаты бронь отменится через {ttl} мин."),
    },
    "payment_link": {
        "ru": ("📋 Бронь:\n{slots}\n\n"
               "⚠️ {reason}\n"
               "Оплатите аванс {amount} по ссылке:\n{link}\n\n"
               "После оплаты отправьте чек в этот чат — администратор подтвердит бронь."),
    },
    "consent_expired": {
        "ru": ("⏰ Бронь снята — подтверждение не получено.\n\n"
               "{slots}\n\n"
               "Слот снова свободен. Хотите забронировать заново? Напишите нам!"),
    },
    "ttl_unpaid": {
        "ru": ("⏰ Бронь снята — оплата не поступила вовремя.\n\n"
               "{slots}\n\n"
               "Слот снова свободен. Хотите забронировать заново? Напишите нам!"),
    },
    "apipay_refunded": {
        "ru": ("↩️ Возврат оформлен: {amount}.\n\n"
               "Деньги вернутся на карту, с которой была оплата. "
               "Если возникнут вопросы — напишите нам."),
    },
}


# Message → (template purpose, its body variables in order). The variables are
# built from the same fields as the text; docs/whatsapp_templates.md holds the
# template wording each purpose expects. Messages absent here (refunds,
# contract receipts) only ever go out as free-form text.
TEMPLATES: dict[str, tuple[str, "callable"]] = {
    "manager_cancelled": ("booking_cancelled", lambda f: [f["slots"], "бронь отменена администратором"]),
    "manager_series_cancelled": ("booking_cancelled", lambda f: [f["slots"], "серия броней отменена администратором"]),
    "manager_unpaid": ("booking_cancelled", lambda f: [f["slots"], "оплата не поступила"]),
    "apipay_expired": ("booking_cancelled", lambda f: [f["slots"], "истёк срок оплаты счёта"]),
    "apipay_failed": ("booking_cancelled", lambda f: [f["slots"], "оплата не прошла"]),
    "consent_expired": ("booking_cancelled", lambda f: [f["slots"], "подтверждение не получено"]),
    "ttl_unpaid": ("booking_cancelled", lambda f: [f["slots"], "оплата не поступила вовремя"]),
    "manager_confirmed": ("booking_confirmed", lambda f: [f["slots"]]),
    "apipay_paid": ("booking_confirmed", lambda f: [f.get("slots") or f"аванс {f['amount']} получен"]),
    # The approved template has one variable: the booking as it is now.
    "booking_edited": ("booking_edited", lambda f: [f["new"]]),
    "booking_created_invoice": ("booking_created", lambda f: [
        f["slots"], f"счёт на аванс {f['amount']} отправлен в Kaspi на номер {f['kaspi_phone']}"]),
    "payment_link": ("payment_link", lambda f: [f["slots"], f["amount"], f["link"]]),
    "contract_payment_link": ("payment_link", lambda f: [
        f"договор №{f['contract_id']}: {f['description']}", f["amount"], f["link"]]),
}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_MONTHS_RU = ("января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря")


def _fmt_time(value) -> str:
    """'20:00:00' / time(20, 0) → '20:00'."""
    return str(value)[:5]


def fmt_slot(booking: dict) -> str:
    """One booking as a line a client can check against their calendar."""
    date = booking.get("date")
    try:
        day = f"{date.day} {_MONTHS_RU[date.month - 1]}"
    except AttributeError:  # already a string (sheet/API round-trip)
        day = str(date)
    times = f"{_fmt_time(booking.get('time_start'))}–{_fmt_time(booking.get('time_end'))}"
    return f"📅 {day}, {times} · поле {booking.get('field')}"


# A WhatsApp message is read on a phone. A weekly slot booked for a year is 52
# occurrences, and a client who scrolls past forty dates to reach the sentence
# that matters has been told nothing.
_MAX_LISTED_SLOTS = 8


def fmt_slots(bookings: list[dict]) -> str:
    """The slot block of a message. Empty input renders nothing, not an empty
    bullet — a caller with no rows to describe still sends a valid message."""
    rows = [b for b in bookings if b]
    listed = "\n".join(fmt_slot(b) for b in rows[:_MAX_LISTED_SLOTS])
    hidden = len(rows) - _MAX_LISTED_SLOTS
    if hidden > 0:
        listed += f"\n… и ещё {hidden}"
    return listed


def fmt_amount(value) -> str:
    """10000 → '10 000₸' (non-breaking-free, matches the bot's other money text)."""
    try:
        return f"{int(float(value)):,}₸".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def render(key: str, lang: str | None = None, **fields) -> str:
    """Render `key`. The catalogue is Russian-only, so `lang` currently only
    matters if a second language is added back to `MESSAGES`.

    A language with no entry falls back to Russian rather than raising: a bad
    `notify_lang` — or one left over from when Kazakh was carried — must not
    cost the client their notification.
    """
    texts = MESSAGES[key]
    return texts.get(lang or "", texts["ru"]).format(**fields)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def normalize_recipient(phone: str | None) -> str | None:
    """A number in E.164 ('+77001234567'), which is what the providers want.

    YCloud rejects anything else outright — `PARAM_INVALID: Invalid E.164 phone
    number: 87476740954` — and the numbers reaching this module are mostly NOT
    in that form. `apipay_invoices.phone` is stored as ApiPay's strict '8XXXX…',
    booking rows hold whatever a manager typed into the sheet, and only the ids
    taken from an inbound message are already correct.

    The 8 → +7 rewrite is the Kazakh (and Russian) trunk prefix, the same
    assumption `apipay_client.normalize_phone` makes in the other direction. A
    number that is already international is passed through untouched, so a
    foreign one only breaks if it is 11 digits starting with 8 — which no KZ
    client's is.
    """
    digits = re.sub(r"\D", "", str(phone or ""))
    if not digits:
        return None
    if len(digits) == 10 and digits.startswith("7"):
        digits = "7" + digits            # 7001234567   → 77001234567
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]        # 87001234567  → 77001234567
    return "+" + digits


def touch_inbound(phone: str | None) -> None:
    """Remember that `phone` just wrote to us (opens the 24h window). Never raises."""
    digits = _window_key(phone)
    if not digits or test_context.is_test_mode():
        return
    try:
        from integrations.repo.utils import _conn
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO whatsapp_last_inbound (phone, at) VALUES (%s, NOW()) "
                    "ON CONFLICT (phone) DO UPDATE SET at = NOW()", (digits,))
    except Exception:  # noqa: BLE001
        logger.debug("[NOTIFY] whatsapp_last_inbound не обновлён для %s", digits, exc_info=True)


def within_service_window(phone: str | None) -> bool | None:
    """Whether `phone` wrote within the last 24h (with a 30-min margin);
    None when we never saw them or the lookup failed."""
    try:
        from integrations.repo.utils import _conn
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT at > NOW() - INTERVAL '23 hours 30 minutes' "
                            "  FROM whatsapp_last_inbound WHERE phone = %s",
                            (_window_key(phone),))
                row = cur.fetchone()
                return None if row is None else bool(row[0])
    except Exception:  # noqa: BLE001
        logger.debug("[NOTIFY] окно 24ч не проверено для %s", phone, exc_info=True)
        return None


def _window_key(phone: str | None) -> str:
    return (normalize_recipient(phone) or "").lstrip("+")


# Template variables are built in Russian. When the Kazakh version of a
# template is sent, the fixed vocabulary they are made of is translated here —
# months, "поле N", the cancellation reasons, the payment line. Anything else
# (customer names, phone numbers, amounts) is language-neutral already.
_MONTHS_KK = ("қаңтар", "ақпан", "наурыз", "сәуір", "мамыр", "маусым",
              "шілде", "тамыз", "қыркүйек", "қазан", "қараша", "желтоқсан")
_KK_PHRASES = (   # longest first: "оплата не поступила вовремя" ⊃ "оплата не поступила"
    ("серия броней отменена администратором", "брондаулар сериясын администратор жойды"),
    ("бронь отменена администратором", "брондауды администратор жойды"),
    ("оплата не поступила вовремя", "төлем уақытында түспеді"),
    ("оплата не поступила", "төлем түспеді"),
    ("истёк срок оплаты счёта", "шотты төлеу мерзімі өтті"),
    ("оплата не прошла", "төлем өтпеді"),
    ("подтверждение не получено", "растау алынбады"),
)


def localize_param(text: str, lang: str | None) -> str:
    """A template variable in the template's language (RU source → KK)."""
    if lang != "kk":
        return text
    out = str(text)
    for ru, kk in _KK_PHRASES:
        out = out.replace(ru, kk)
    out = re.sub(r"счёт на аванс (.+?) отправлен в Kaspi на номер (\S.*)",
                 r"\1 аванс шоты Kaspi-ге (\2) жіберілді", out)
    out = re.sub(r"аванс (\S+₸) получен", r"\1 аванс алынды", out)
    out = re.sub(r"поле (\d+)", r"\1-алаң", out)
    out = re.sub(r"… и ещё (\d+)", r"… тағы \1", out)
    for ru, kk in zip(_MONTHS_RU, _MONTHS_KK):
        out = re.sub(rf"(\d) {ru}\b", rf"\1 {kk}", out)
    return out


def template_lang(lang: str | None) -> str:
    """Every template is approved in ru and kk; anything else → the default."""
    return lang if lang in ("ru", "kk") else config.WA_TEMPLATE_LANG


def _one_line(value) -> str:
    """Template variables may not contain newlines, tabs or 4+ spaces."""
    text = re.sub(r"\s*\n\s*", "; ", str(value)).replace("\t", " ")
    return re.sub(r" {4,}", "   ", text).strip() or "—"


def deliver(to: str | None, text: str, purpose: str | None = None,
            params: list | None = None, *, provider: str | None = None,
            phone_number_id: str | None = None, label: str = "message",
            lang: str | None = None) -> str:
    """Send `text` — or, outside the 24h window, the `purpose` template.

    The template is used when one is configured for `purpose` and the number
    has not written in the last 24h (or never): free-form would be rejected
    there — YCloud even accepts it and fails later, so it cannot be tried
    first. Without a configured template the free-form text is attempted.
    The template goes out in the client's language (`lang`: ru | kk; else
    WA_TEMPLATE_LANG), its variables translated to match.
    Returns "text" | "template" | "failed"; never raises.
    """
    recipient = normalize_recipient(to)
    if not recipient:
        logger.warning("[NOTIFY] %s не отправлено — нет номера получателя", label)
        return "failed"

    # Backstop independent of the ambient sandbox flag — see whatsapp_client.
    from integrations.repo.agent_test_repo import is_test_phone

    if is_test_phone(recipient):
        logger.error("[NOTIFY] %s не отправлено — номер %s принадлежит тест-консоли", label, recipient)
        return "failed"
    try:
        from handlers import whatsapp_client
        from integrations.providers.payload import OutboundChannel

        channel = OutboundChannel(
            provider=provider or _DEFAULT_PROVIDER,
            phone_number_id=phone_number_id or _default_phone_number_id(),
        )
        template = config.WA_TEMPLATES.get(purpose or "", "")
        if template and within_service_window(recipient) is not True:
            tlang = template_lang(lang)
            try:
                whatsapp_client.send_template_message(
                    channel, recipient, template, tlang,
                    [_one_line(localize_param(p, tlang)) for p in (params or [])])
            except Exception:  # noqa: BLE001
                if tlang == "ru":
                    raise
                # e.g. the kk version of this template is missing or not approved
                logger.exception("[NOTIFY] Шаблон %s/%s не отправлен — пробуем ru",
                                 template, tlang)
                tlang = "ru"
                whatsapp_client.send_template_message(
                    channel, recipient, template, tlang,
                    [_one_line(p) for p in (params or [])])
            logger.info("[NOTIFY] %s → %s (шаблон %s/%s)", label, recipient, template, tlang)
            return "template"
        whatsapp_client.send_text_message(channel, recipient, text)
        logger.info("[NOTIFY] %s → %s", label, recipient)
        return "text"
    except Exception:  # noqa: BLE001 — the reported change is already committed
        logger.exception("[NOTIFY] Не удалось отправить %s на %s", label, recipient)
        return "failed"


def send(to: str | None, key: str, lang: str | None = None, *,
         provider: str | None = None, phone_number_id: str | None = None,
         **fields) -> bool:
    """Send one rendered message (or its template). Returns whether it went
    out; never raises."""
    purpose, params = None, None
    if key in TEMPLATES:
        purpose, build = TEMPLATES[key]
        try:
            params = build(fields)
        except KeyError:
            logger.warning("[NOTIFY] %s: нет данных для шаблона %s", key, purpose)
            purpose = None
    return deliver(to, render(key, lang, **fields), purpose, params,
                   provider=provider, phone_number_id=phone_number_id,
                   label=key, lang=lang) != "failed"


def alert_admin(reason: str, client: str, booking: str, details: str = "") -> str:
    """Tell the admin (ADMIN_ALERT_PHONE) that a booking is being paid by link
    and needs a person.

    `reason`, `client`, `booking` are also the `admin_alert` template's {{1}},
    {{2}}, {{3}} — three variables in a long fixed text, which is what Meta
    accepts. `details` only appears in the free-form version.
    Never raises; returns the delivery like `deliver`.
    """
    text = (f"⚠️ Dopsy Arena — нужна помощь администратора.\n"
            f"Что случилось: {reason}.\n"
            f"Клиент: {client}.\n"
            f"Бронь: {booking}.\n\n"
            f"Клиенту отправлена ссылка на оплату аванса. Свяжитесь с клиентом, "
            f"проверьте чек и подтвердите бронь.\n{details}").rstrip()
    logger.warning("[NOTIFY] Админу: %s", text.replace("\n", " | "))
    if not config.ADMIN_ALERT_PHONE:
        logger.error("[NOTIFY] ADMIN_ALERT_PHONE не задан — администратор не уведомлён")
        return "failed"
    return deliver(config.ADMIN_ALERT_PHONE, text, "admin_alert",
                   [reason, client, booking], label="admin_alert")


def send_async(to: str | None, key: str, lang: str | None = None, **kwargs) -> None:
    """`send` off the caller's thread.

    Used by the manager API (its response must not wait on WhatsApp) and by the
    ApiPay webhook, which has ~5 seconds before ApiPay starts retrying.
    """
    # spawn_thread (not threading.Thread) so the agent-test sandbox flag
    # survives into the child — otherwise a console turn would send a real
    # WhatsApp notification to a real customer.
    test_context.spawn_thread(send, to, key, lang, **kwargs)


# ---------------------------------------------------------------------------
# Booking-shaped helpers
# ---------------------------------------------------------------------------

def notify_booking(booking: dict | None, key: str, lang: str | None = None,
                   **fields) -> None:
    """Tell the client of `booking` about `key`, describing the slot itself.

    A no-op for a booking with no phone on it (manager rows created for walk-ins
    routinely have none) — nothing to send to, and nothing worth an error.
    """
    if not booking:
        return
    send_async(booking.get("phone"), key, lang,
               slots=fmt_slot(booking), **fields)


def notify_bookings(bookings: list[dict], key: str, lang: str | None = None,
                    **fields) -> None:
    """One message per client for a set of bookings — a batch invoice covers
    several slots, and the client wants them in a single message, not five."""
    by_phone: dict[str, list[dict]] = {}
    for b in bookings:
        phone = normalize_recipient((b or {}).get("phone"))
        if phone:
            by_phone.setdefault(phone, []).append(b)
    for phone, rows in by_phone.items():
        send_async(phone, key, lang, slots=fmt_slots(rows), **fields)
