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

Language: bot-created invoices carry the language the client actually used
(`notify_lang`), so those answer in it. Manager actions carry no language at all
— nobody asked the client anything — so those go out bilingual (RU + KK), the
same shape as the reservation-TTL notice in `app.py`.
"""

import logging
import re
import threading

import config

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
# One entry per event, `{token}` formatted by the caller. Kazakh is not
# optional here: these are the messages a client gets without having written to
# us first, so the one they can read has to be in the message itself.

MESSAGES: dict[str, dict[str, str]] = {
    # ── Manager actions ───────────────────────────────────────────────────
    "manager_cancelled": {
        "ru": ("❌ Ваша бронь отменена.\n\n"
               "{slots}\n\n"
               "Если это ошибка — напишите нам, мы поможем."),
        "kk": ("❌ Брондауыңыз жойылды.\n\n"
               "{slots}\n\n"
               "Қате болса — бізге жазыңыз, көмектесеміз."),
    },
    "manager_series_cancelled": {
        "ru": ("❌ Все повторяющиеся брони отменены.\n\n"
               "{slots}\n\n"
               "Если это ошибка — напишите нам, мы поможем."),
        "kk": ("❌ Барлық қайталанатын брондаулар жойылды.\n\n"
               "{slots}\n\n"
               "Қате болса — бізге жазыңыз, көмектесеміз."),
    },
    "manager_confirmed": {
        "ru": ("✅ Ваша бронь подтверждена!\n\n"
               "{slots}\n\n"
               "До встречи на поле! ⚽"),
        "kk": ("✅ Брондауыңыз расталды!\n\n"
               "{slots}\n\n"
               "Алаңда кездескенше! ⚽"),
    },
    "manager_unpaid": {
        "ru": ("⚠️ Бронь снята — оплата не поступила.\n\n"
               "{slots}\n\n"
               "Слот снова свободен. Хотите забронировать заново? Напишите нам!"),
        "kk": ("⚠️ Брондау алынып тасталды — төлем түспеді.\n\n"
               "{slots}\n\n"
               "Уақыт қайта бос. Қайта брондау үшін бізге жазыңыз!"),
    },

    # ── ApiPay ────────────────────────────────────────────────────────────
    "apipay_paid": {
        "ru": ("✅ Оплата получена — бронь подтверждена!\n\n"
               "💰 Аванс: {amount}\n"
               "⚠️ Возврат при неявке не производится.\n\n"
               "До встречи на поле! ⚽"),
        "kk": ("✅ Төлем қабылданды — брондау расталды!\n\n"
               "💰 Аванс: {amount}\n"
               "⚠️ Келмесеңіз төлем қайтарылмайды.\n\n"
               "Алаңда кездескенше! ⚽"),
    },
    "apipay_expired": {
        "ru": ("⏰ Срок оплаты счёта истёк — бронь снята.\n\n"
               "{slots}\n\n"
               "Слот снова свободен. Хотите забронировать заново? Напишите нам!"),
        "kk": ("⏰ Шот төлеу мерзімі өтті — брондау алынып тасталды.\n\n"
               "{slots}\n\n"
               "Уақыт қайта бос. Қайта брондау үшін бізге жазыңыз!"),
    },
    "apipay_failed": {
        "ru": ("❌ Оплата не прошла — бронь снята.\n\n"
               "{slots}\n\n"
               "Слот снова свободен. Попробуйте забронировать заново — "
               "или напишите нам, если нужна помощь."),
        "kk": ("❌ Төлем өтпеді — брондау алынып тасталды.\n\n"
               "{slots}\n\n"
               "Уақыт қайта бос. Қайта брондап көріңіз — "
               "немесе көмек керек болса, бізге жазыңыз."),
    },
    "apipay_refunded": {
        "ru": ("↩️ Возврат оформлен: {amount}.\n\n"
               "Деньги вернутся на карту, с которой была оплата. "
               "Если возникнут вопросы — напишите нам."),
        "kk": ("↩️ Қайтарым рәсімделді: {amount}.\n\n"
               "Ақша төлем жасалған картаға қайтарылады. "
               "Сұрақ туындаса — бізге жазыңыз."),
    },
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
        listed += f"\n… и ещё {hidden} / тағы {hidden}"
    return listed


def fmt_amount(value) -> str:
    """10000 → '10 000₸' (non-breaking-free, matches the bot's other money text)."""
    try:
        return f"{int(float(value)):,}₸".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def render(key: str, lang: str | None = None, **fields) -> str:
    """Render `key`. `lang=None` → RU and KK in one message.

    An unknown language falls back to Russian rather than raising: a bad
    `notify_lang` must not cost the client their notification.
    """
    texts = MESSAGES[key]
    if lang in texts:
        return texts[lang].format(**fields)
    return f"{texts['ru'].format(**fields)}\n\n{texts['kk'].format(**fields)}"


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


def send(to: str | None, key: str, lang: str | None = None, *,
         provider: str | None = None, phone_number_id: str | None = None,
         **fields) -> bool:
    """Send one rendered message. Returns whether it went out; never raises."""
    recipient = normalize_recipient(to)
    if not recipient:
        logger.warning("[NOTIFY] %s не отправлено — нет номера получателя", key)
        return False
    try:
        from handlers.whatsapp_client import send_text_message
        from integrations.providers.payload import OutboundChannel

        channel = OutboundChannel(
            provider=provider or _DEFAULT_PROVIDER,
            phone_number_id=phone_number_id or _default_phone_number_id(),
        )
        send_text_message(channel, recipient, render(key, lang, **fields))
        logger.info("[NOTIFY] %s → %s", key, recipient)
        return True
    except Exception:  # noqa: BLE001 — the reported change is already committed
        logger.exception("[NOTIFY] Не удалось отправить %s клиенту %s", key, recipient)
        return False


def send_async(to: str | None, key: str, lang: str | None = None, **kwargs) -> None:
    """`send` off the caller's thread.

    Used by the manager API (its response must not wait on WhatsApp) and by the
    ApiPay webhook, which has ~5 seconds before ApiPay starts retrying.
    """
    threading.Thread(target=send, args=(to, key, lang), kwargs=kwargs,
                     daemon=True).start()


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
