"""Offer consent before the ApiPay avans invoice.

No Kaspi invoice is raised until the client has agreed to the public offer
(the avans is refundable within 48 hours of payment). Every path that wants money goes through here:

    bot "Подтверждаю"        ─┐
    manager batch / single   ─┼─► bookings held in `awaiting_consent` (slot blocked,
                              │    no invoice) + a `consent_requests` row
                              │    manager-created first: "confirm this booking?"
                              │      «✅ Подтверждаю / ❌ Отменить» (the
                              │      `booking_confirm_request` template when the client
                              │      has not written in 24h); the tap opens the window
                              │    → the client gets "✅ Келісемін / ❌ Жоқ"
    website (consent ticked) ─┴─► consent stored with the booking, invoice at once

    ❌ Жоқ / no answer in CONSENT_TTL_SECONDS → bookings cancelled, nothing billed
    ✅ Келісемін → consent_at / channel / versions saved on every booking, then
                   the PAYMENT number (the contact number is `phone`):
                     the one the manager entered            → used as is
                     else customers.kaspi_phone (last time) → "same number?"
                     else                                   → the WhatsApp number
    payment number checked in Kaspi (ensure_kaspi_client)
      found      → `issue_invoice`: awaiting_payment + invoice sent; the number
                   is saved on the booking and the client's profile
      not found  → "Бұл нөмірде Kaspi табылмады… қай нөмірге жіберейін?" — the
                   typed number is format-checked (a bad format is re-asked and
                   not counted) and tried; at most KASPI_PHONE_MAX_RETRIES asks
      still none, or the check itself failed
                 → `switch_to_link`: «Администратор сізбен байланысады», the
                   admin is alerted, the client also gets the Kaspi payment link
                   (awaiting_payment, slot held LINK_PAYMENT_TTL_SECONDS)

The tap on a template button is an inbound message, so it opens the 24h window
and everything after it can be free-form.

The guard that makes this binding is in `apipay_service.queue_invoice`: it
refuses any booking whose `consent_at` is NULL. Contract instalments write
their invoice rows directly and are not covered (open question).

Dialog state lives in `consent_requests` (migration 063), keyed by the client's
WhatsApp number; the proof lives on the booking rows.
"""

import json
import logging
import re

import psycopg2.extras

import config
from integrations import apipay_client, client_notify, test_context
from integrations.apipay_client import ApiPayError, KaspiClientMissing
from integrations.repo.history_repo import _record_history
from integrations.repo.utils import _conn, normalize_phone, phone_variants
from integrations.status_labels import STATES_RUSSIAN as _STATES_RUSSIAN

logger = logging.getLogger(__name__)

# Steps in which the bot is talking to the client and the bookings are held.
ACTIVE_STEPS = ("confirm", "offer", "same_number", "new_number")
# Steps whose bookings an admin may still bill with a Kaspi invoice.
BILLABLE_STEPS = ACTIVE_STEPS + ("link",)

_HISTORY_SOURCE = "chatbot:Бот"

REASON_DECLINED = "consent_declined"
REASON_EXPIRED = "consent_expired"
REASON_BOOKING_DECLINED = "booking_declined"

# Why the client is paying by link, as shown to them and to the admin.
_LINK_REASONS = {
    "no_kaspi": "На номере {phone} не найден Kaspi — счёт выставить нельзя.",
    "check_failed": "Не удалось выставить счёт в Kaspi на номер {phone}.",
}


# ---------------------------------------------------------------------------
# Texts (the in-dialog replies — the client has just written, so free-form)
# ---------------------------------------------------------------------------

T = {
    # The task's wording (KK) and its RU translation.
    "prompt": {
        "kk": ("⚠️ Маңызды ереже. Брондау үшін {avans} аванс төленеді. "
               "Авансты төлегеннен кейін 48 сағат ішінде қайтаруға болады. Толық шарттар — офертада: {offer_url}. "
               "Төлем жасау арқылы сіз осы офертамен келісетініңізді растайсыз.\n\n"
               "Келісесіз бе? Келіссеңіз, Kaspi-ге төлем шоты келеді."),
        "ru": ("⚠️ Важное правило. Для брони вносится аванс {avans}. "
               "Аванс можно вернуть в течение 48 часов после оплаты. Полные условия — в оферте: {offer_url}. "
               "Оплачивая, вы подтверждаете, что согласны с офертой.\n\n"
               "Согласны? Если согласны, в Kaspi придёт счёт на оплату."),
    },
    "prompt_slots": {"kk": "📋 Брондау:\n{slots}\n\n", "ru": "📋 Бронь:\n{slots}\n\n"},
    # Manager-created bookings: the booking itself is confirmed first.
    "confirm_request": {
        "kk": ("Сәлеметсіз бе! Dopsy Arena администраторы сізге брондау жасады:\n\n{slots}\n\n"
               "Брондауды растайсыз ба? Жауап болмаса, ол {ttl} минуттан кейін жойылады."),
        "ru": ("Здравствуйте! Администратор Dopsy Arena оформил для вас бронь:\n\n{slots}\n\n"
               "Подтверждаете бронь? Без ответа она снимется через {ttl} минут."),
    },
    # Same words as the approved template's quick replies.
    "btn_confirm": {"kk": "✅ Растау", "ru": "✅ Подтвердить"},
    "btn_cancel": {"kk": "❌ Болдырмау", "ru": "❌ Отменить"},
    "booking_declined": {
        "kk": "Түсінікті — брондау жойылды, слот босатылды. Қаласаңыз қайта жазыңыз! 🙂",
        "ru": "Понятно — бронь отменена, слот освобождён. Если передумаете — напишите! 🙂",
    },
    "btn_yes": {"kk": "✅ Келісемін", "ru": "✅ Согласен"},
    "btn_no": {"kk": "❌ Жоқ", "ru": "❌ Нет"},
    "same_number": {
        "kk": "Шотты өткендегідей {phone} нөміріне жіберейін бе?",
        "ru": "Отправить счёт, как в прошлый раз, на номер {phone}?",
    },
    "btn_same_yes": {"kk": "✅ Иә", "ru": "✅ Да"},
    "btn_same_no": {"kk": "❌ Басқа нөмір", "ru": "❌ Другой номер"},
    "ask_number": {
        "kk": "Kaspi шоты жіберілетін нөмірді +7 7XX XXX XX XX форматында жазыңыз.",
        "ru": "Напишите номер для счёта Kaspi в формате +7 7XX XXX XX XX.",
    },
    # The task's wording: the number just tried has no Kaspi — ask for another.
    "no_kaspi_ask": {
        "kk": ("Бұл нөмірде Kaspi табылмады. Төлем шотын қай нөмірге жіберейін? "
               "Нөмірді +7 7XX XXX XX XX форматында жазыңыз."),
        "ru": ("На этом номере Kaspi не найден. На какой номер отправить счёт? "
               "Напишите номер в формате +7 7XX XXX XX XX."),
    },
    "bad_format": {
        "kk": "Нөмірді тани алмадым. +7 7XX XXX XX XX форматында жазыңыз.",
        "ru": "Не удалось распознать номер. Напишите его в формате +7 7XX XXX XX XX.",
    },
    "declined": {
        "kk": "Түсінікті — брондау тоқтатылды, шот жіберілмейді. Қаласаңыз қайта жазыңыз! 🙂",
        "ru": "Понятно — бронь отменена, счёт не выставлен. Если передумаете — напишите! 🙂",
    },
    "invoice_sent": {
        "kk": ("💳 {avans} аванс шоты Kaspi-ге ({phone}) жіберілді — төлемді "
               "Kaspi қосымшасында растаңыз.\n\n{slots}\n\n"
               "Қалған сома орында төленеді.\n"
               "⚠️ {ttl} мин төлемсіз — брондау жойылады."),
        "ru": ("💳 Счёт на аванс {avans} отправлен в Kaspi ({phone}) — подтвердите "
               "оплату в приложении Kaspi.\n\n{slots}\n\n"
               "Остаток оплачивается на месте.\n"
               "⚠️ {ttl} мин без оплаты — бронь отменится."),
    },
    "pay_by_link": {
        "kk": ("⚠️ Kaspi тіркелген нөмір табылмады. Администратор сізбен байланысады — "
               "слот сізге сақталады.\n\n"
               "{slots}\n\n"
               "Күткіңіз келмесе, {avans} авансты мына сілтеме арқылы төлеп, чекті осы "
               "чатқа жіберіңіз:\n{link}"),
        "ru": ("⚠️ Номер с Kaspi не найден. Администратор свяжется с вами — "
               "слот пока за вами.\n\n"
               "{slots}\n\n"
               "Если не хотите ждать — оплатите аванс {avans} по ссылке и отправьте "
               "чек в этот чат:\n{link}"),
    },
    "check_failed_link": {
        "kk": ("⚠️ {phone} нөміріне Kaspi шотын жіберу мүмкін болмады. Администратор "
               "сізбен байланысады — слот сізге сақталады.\n\n"
               "{slots}\n\n"
               "Күткіңіз келмесе, {avans} авансты мына сілтеме арқылы төлеп, чекті осы "
               "чатқа жіберіңіз:\n{link}"),
        "ru": ("⚠️ Не удалось выставить счёт в Kaspi на номер {phone}. Администратор "
               "свяжется с вами — слот пока за вами.\n\n"
               "{slots}\n\n"
               "Если не хотите ждать — оплатите аванс {avans} по ссылке и отправьте "
               "чек в этот чат:\n{link}"),
    },
    "send_failed": {
        "kk": ("⚠️ Аванс шотын жасау мүмкін болмады.\n{error}\n\n"
               "Брондау жойылды — слот бос.\n📞 Менеджерге қоңырау шалыңыз, ол қолмен брондайды."),
        "ru": ("⚠️ Не удалось выставить счёт на аванс.\n{error}\n\n"
               "Бронь снята — слот свободен.\n📞 Позвоните менеджеру, он забронирует вручную."),
    },
    "no_invoice": {
        "kk": "✅ Рахмет! Бұл брондау үшін аванс талап етілмейді.",
        "ru": "✅ Спасибо! Для этой брони аванс не требуется.",
    },
    "gone": {
        "kk": "Бұл брондау енді белсенді емес. Қайта брондағыңыз келсе, жазыңыз!",
        "ru": "Эта бронь уже не активна. Хотите забронировать снова? Напишите!",
    },
}


def _t(key: str, lang: str | None, **fields) -> str:
    texts = T[key]
    return texts.get(lang or "ru", texts["ru"]).format(**fields)


def canonical_phone(phone: str | None) -> str:
    """Digits in the 7XXXXXXXXXX form a WhatsApp sender id has.

    The dialog is keyed by the client's number, and it arrives in several
    spellings: '+7700…' / '7700…' from WhatsApp, '8 700 …' typed by a manager.
    """
    digits = normalize_phone(phone)
    if len(digits) == 11 and digits.startswith("8"):
        return "7" + digits[1:]
    if len(digits) == 10 and digits.startswith("7"):
        return "7" + digits
    return digits


def _phone_keys(phone: str | None) -> list[str]:
    """Every stored spelling of `phone` worth matching (7…, 8…, +7…)."""
    canon = canonical_phone(phone)
    keys = set(phone_variants(canon))
    if len(canon) == 11 and canon.startswith("7"):
        keys.add("8" + canon[1:])
    return sorted(keys)


def _buttons(text: str, titles: list[str]) -> str:
    """Interactive reply-button message, in the JSON form send_text_message sends as such."""
    return json.dumps({
        "type": "button",
        "body": {"text": text},
        "action": {"buttons": [
            {"type": "reply", "reply": {"id": f"consent_{i}", "title": title}}
            for i, title in enumerate(titles, start=1)
        ]},
    }, ensure_ascii=False)


def _fmt_amount(value) -> str:
    return client_notify.fmt_amount(value)


def display_phone(phone: str | None) -> str:
    """'87071234567' → '+7 707 123 45 67' — how the client writes it."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits[0] in "78":
        d = digits[1:]
        return f"+7 {d[:3]} {d[3:6]} {d[6:8]} {d[8:]}"
    return phone or "—"


def confirm_message(lang: str | None, bookings: list[dict]) -> str:
    """Manager-created booking: "confirm this booking?" with its two buttons."""
    return _buttons(_t("confirm_request", lang, slots=client_notify.fmt_slots(bookings),
                       ttl=config.CONSENT_TTL_SECONDS // 60),
                    [_t("btn_confirm", lang), _t("btn_cancel", lang)])


def prompt_message(lang: str | None, amount, bookings: list[dict] | None = None) -> str:
    """The consent question with its two buttons. `bookings` is shown on top
    when the client has not just seen the booking summary (manager-created)."""
    body = ""
    if bookings:
        body += _t("prompt_slots", lang, slots=client_notify.fmt_slots(bookings))
    body += _t("prompt", lang, avans=_fmt_amount(amount), offer_url=config.OFFER_URL)
    return _buttons(body, [_t("btn_yes", lang), _t("btn_no", lang)])


# ---------------------------------------------------------------------------
# Answer parsing
# ---------------------------------------------------------------------------

def _word_re(words: list[str]) -> re.Pattern:
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in words) + r")(?!\w)")


# Refusals are checked first: "не согласен" contains "согласен". A refusal at
# the offer step cancels the booking, so only unambiguous words count — a bare
# "не" ("не знаю") or "другой" ("другой день") just re-asks the question.
_NO_PHRASES = ("не соглас", "келіспей", "келиспей")
# "Another number" — only meaningful as an answer to "same number as last time?".
_OTHER_NUMBER_PHRASES = ("басқа нөмір", "баска номер", "басқа", "другой номер", "другой")
_NO_RE = _word_re(["жоқ", "жок", "нет", "no", "отмена", "отменить", "бас тартамын"])
_YES_RE = _word_re(["келісемін", "келисемин", "келісем", "согласен", "согласна",
                    "согласны", "иә", "ия", "иа", "да", "yes", "ok", "ок",
                    "жарайды", "дұрыс"])


_CANCEL_PHRASES = ("отменить", "отмена", "бас тарт", "болдырма", "не подтверж", "растамаймын")
_CONFIRM_NO_RE = _word_re(["нет", "жоқ", "жок", "no"])
_CONFIRM_YES_RE = _word_re(["подтверждаю", "подтвердить", "растаймын", "растау", "да",
                            "иә", "ия", "иа", "ok", "ок", "yes"])


def parse_confirm(text: str) -> str | None:
    """'yes' / 'no' / None for "do you confirm this booking?"."""
    lower = (text or "").lower().strip()
    if not lower:
        return None
    if "❌" in lower or any(p in lower for p in _CANCEL_PHRASES) or _CONFIRM_NO_RE.search(lower):
        return "no"
    if _CONFIRM_YES_RE.search(lower):
        return "yes"
    return None


def parse_answer(text: str, same_number: bool = False) -> str | None:
    """'yes' / 'no' / None for a consent (or, with `same_number`, a
    "bill the same number?") answer."""
    lower = (text or "").lower().strip()
    if not lower:
        return None
    if same_number and any(p in lower for p in _OTHER_NUMBER_PHRASES):
        return "no"
    if "❌" in lower or any(p in lower for p in _NO_PHRASES) or _NO_RE.search(lower):
        return "no"
    # Words only, never the ✅ alone: the booking summary's own button
    # («Подтверждаю✅») tapped twice must not read as agreeing to the offer.
    if _YES_RE.search(lower):
        return "yes"
    return None


def parse_phone(text: str) -> str | None:
    """A Kaspi-shaped number in `text`, in ApiPay's 8XXXXXXXXXX form, or None."""
    digits = re.sub(r"\D", "", text or "")
    if not digits:
        return None
    try:
        return apipay_client.normalize_phone(digits)
    except ApiPayError:
        return None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def create_request(cur, *, phone: str, booking_ids: list[int],
                   chargeable_ids: list[int], slot_count: int, source: str,
                   consent_channel: str, lang: str | None = None,
                   chat_id: str | None = None, provider: str | None = None,
                   amount=None, description: str | None = None,
                   kaspi_phone: str | None = None,
                   ttl_seconds: int | None = None, step: str = "offer") -> int:
    """Open a consent request on the caller's cursor (inside the transaction
    that put the bookings into `awaiting_consent`). Returns its id.

    `phone` is the CONTACT number (who is asked); `kaspi_phone` the PAYMENT
    number when the manager gave one. The bookings' `reserved_until` is aligned
    with the request's expiry so the sheet and the manager UI show the deadline
    the sweeper enforces.
    """
    ttl = ttl_seconds or config.CONSENT_TTL_SECONDS
    preset = parse_phone(kaspi_phone) if kaspi_phone else None
    cur.execute(
        "INSERT INTO consent_requests "
        "  (phone, booking_ids, chargeable_ids, slot_count, amount, description, "
        "   source, consent_channel, lang, chat_id, provider, kaspi_phone, step, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "        NOW() + make_interval(secs => %s)) "
        "RETURNING id, expires_at",
        (canonical_phone(phone), list(booking_ids), list(chargeable_ids), slot_count,
         amount, description, source, consent_channel, lang, chat_id, provider,
         preset, step, ttl),
    )
    row = cur.fetchone()
    req_id = row["id"] if isinstance(row, dict) else row[0]
    expires_at = row["expires_at"] if isinstance(row, dict) else row[1]
    cur.execute(
        "UPDATE bookings SET reserved_until = %s "
        " WHERE id = ANY(%s) AND reserved_until IS NOT NULL",
        (expires_at, list(booking_ids)),
    )
    for bid in booking_ids:
        cur.execute(
            "INSERT INTO booking_events (booking_id, event, actor_type, note) "
            "VALUES (%s, 'consent_requested', 'system', %s)",
            (bid, f"consent_request={req_id} channel={consent_channel}"),
        )
    logger.info("[CONSENT] Запрос согласия #%s для броней %s (канал %s)",
                req_id, list(booking_ids), consent_channel)
    return req_id


def start_hold_payment(booking_id: int, *, phone: str | None = None,
                       kaspi_phone: str | None = None, lang: str | None = None,
                       source: str = "manager") -> dict:
    """Convert a no-payment hold into the ordinary manager consent flow.

    This never issues an invoice. It changes the held logical slot to
    ``awaiting_consent`` and asks the client to confirm the booking and offer;
    billing can begin only after that dialog succeeds.
    """
    contact = canonical_phone(phone)
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, phone, customer_name, state, payment_method, group_transition "
                "FROM bookings WHERE id = %s FOR UPDATE",
                (booking_id,),
            )
            booking = cur.fetchone()
            if not booking:
                return {"ok": False, "code": "NOT_FOUND", "message": "Бронь не найдена."}
            if booking["state"] != "awaiting_payment" or booking["payment_method"] != "hold_without_payment":
                return {"ok": False, "code": "NOT_A_HOLD",
                        "message": "Бронь не является 24-часовым удержанием без оплаты."}

            contact = contact or canonical_phone(booking.get("phone"))
            if len(contact) != 11 or not contact.startswith("7"):
                return {"ok": False, "code": "PHONE_REQUIRED",
                        "message": "Для запроса оплаты нужен номер клиента."}

            cur.execute(
                "SELECT id FROM bookings WHERE id = %s OR "
                " (group_transition IS NOT NULL AND group_transition = %s) "
                "ORDER BY id FOR UPDATE",
                (booking_id, booking.get("group_transition")),
            )
            booking_ids = [row["id"] for row in cur.fetchall()]
            cur.execute(
                "UPDATE bookings SET state = 'awaiting_consent', phone = %s, "
                "payment_method = NULL, paid_avans = 0, updated_at = NOW() "
                "WHERE id = ANY(%s)",
                (contact, booking_ids),
            )
            for held_id in booking_ids:
                _record_history(
                    cur, held_id, source, key="status_change",
                    old_status=_STATES_RUSSIAN.get("awaiting_payment"),
                    new_status=_STATES_RUSSIAN.get("awaiting_consent"),
                )
            req_id = create_request(
                cur, phone=contact, booking_ids=booking_ids,
                chargeable_ids=booking_ids, slot_count=1, source="manager",
                consent_channel="admin_bot", lang=lang,
                chat_id=f"{config.WHATSAPP_PHONE_NUMBER_ID_BOT_1}:{contact}",
                provider="ycloud", kaspi_phone=kaspi_phone, step="confirm",
            )
    return {"ok": True, "request_id": req_id, "booking_ids": booking_ids}


def record_consent(cur, booking_ids: list[int], channel: str,
                   offer_version: str | None = None,
                   text_version: str | None = None) -> None:
    """Store the proof of consent on `booking_ids` (only where not stored yet —
    the first agreement is the one that counts)."""
    cur.execute(
        "UPDATE bookings SET consent_at = NOW(), consent_channel = %s, "
        "       consent_text_version = %s, offer_version = %s, updated_at = NOW() "
        " WHERE id = ANY(%s) AND consent_at IS NULL",
        (channel, text_version or config.CONSENT_TEXT_VERSION,
         offer_version or config.OFFER_VERSION, list(booking_ids)),
    )
    for bid in booking_ids:
        cur.execute(
            "INSERT INTO booking_events (booking_id, event, actor_type, note) "
            "VALUES (%s, 'consent_given', %s, %s)",
            (bid, channel, f"offer={offer_version or config.OFFER_VERSION} "
                           f"text={text_version or config.CONSENT_TEXT_VERSION}"),
        )


def remember_kaspi_phone(client_phone: str | None, kaspi_phone: str,
                         name: str | None = None) -> None:
    """customers.kaspi_phone ← the number just billed successfully. Never raises.

    Every booking already has a `customers` row — the bookings trigger from
    migration 053 creates one from the booking's phone, bot bookings included —
    so this only updates it (matching any spelling of the number).
    """
    if not client_phone:
        return
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE customers SET kaspi_phone = %s, updated_at = NOW() "
                    " WHERE phone = ANY(%s)",
                    (kaspi_phone, _phone_keys(client_phone)),
                )
                if cur.rowcount == 0:
                    logger.warning("[CONSENT] Профиль клиента %s не найден — "
                                   "kaspi_phone не сохранён", client_phone)
    except Exception:  # noqa: BLE001
        logger.exception("[CONSENT] Не удалось сохранить kaspi_phone клиента %s", client_phone)


def last_kaspi_phone(client_phone: str) -> str | None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT kaspi_phone FROM customers WHERE phone = ANY(%s) "
                "   AND kaspi_phone IS NOT NULL LIMIT 1",
                (_phone_keys(client_phone),),
            )
            row = cur.fetchone()
            return row[0] if row else None


def get_request(req_id: int) -> dict | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM consent_requests WHERE id = %s", (req_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def get_active_request(phone: str) -> dict | None:
    """The request the bot is in the middle of with this client, if any."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM consent_requests "
                " WHERE phone = ANY(%s) AND step = ANY(%s) "
                " ORDER BY id DESC LIMIT 1",
                (_phone_keys(phone), list(ACTIVE_STEPS)),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def get_billable_request_for_booking(booking_id: int) -> dict | None:
    """The request whose bookings an admin may still bill (held for consent,
    or paying by link)."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM consent_requests "
                " WHERE %s = ANY(booking_ids) AND step = ANY(%s) "
                " ORDER BY id DESC LIMIT 1",
                (booking_id, list(BILLABLE_STEPS)),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def _set_step(req_id: int, step: str, from_steps=ACTIVE_STEPS, **extra) -> bool:
    """Move a request to `step` if it is still in one of `from_steps`."""
    sets = ["step = %s", "updated_at = NOW()"]
    params: list = [step]
    for col, val in extra.items():
        sets.append(f"{col} = %s")
        params.append(val)
    params += [req_id, list(from_steps)]
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE consent_requests SET {', '.join(sets)} "
                " WHERE id = %s AND step = ANY(%s)", params)
            return cur.rowcount > 0


def _held_bookings(booking_ids: list[int]) -> list[dict]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM bookings WHERE id = ANY(%s) AND state = 'awaiting_consent' "
                " ORDER BY date, time_start, id", (list(booking_ids),))
            return [dict(r) for r in cur.fetchall()]


def _bookings(ids: list[int]) -> list[dict]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM bookings WHERE id = ANY(%s) ORDER BY date, time_start, id",
                        (list(ids),))
            return [dict(r) for r in cur.fetchall()]


def _amount_for(req: dict, slot_count: int | None = None):
    if req.get("amount") and slot_count in (None, req["slot_count"]):
        return req["amount"]
    return (slot_count if slot_count is not None else req["slot_count"]) * config.APIPAY_AVANS_PER_BOOKING


def _slot_count(rows: list[dict]) -> int:
    """Bookings an avans is charged for: the two halves of a day-crossing slot
    share a `group_transition` and count once."""
    return len({str(r.get("group_transition") or f"id:{r['id']}") for r in rows})


def _billable(rows: list[dict]) -> list[dict]:
    """Rows of a request that can still be billed with a Kaspi invoice."""
    return [r for r in rows if r["state"] == "awaiting_consent"
            or (r["state"] == "awaiting_payment" and r.get("payment_method") == "link")]


# ---------------------------------------------------------------------------
# Outbound
# ---------------------------------------------------------------------------

def _channel_kwargs(req: dict) -> dict:
    phone_number_id = (req.get("chat_id") or "").partition(":")[0] or None
    return {"provider": req.get("provider") or None, "phone_number_id": phone_number_id}


def deliver_prompt(req_id: int) -> str:
    """Ask the client of a manager-created request to confirm the booking.

    Inside the 24h window → the interactive message; outside it → the
    `booking_confirm_request` template (quick replies «✅ Подтверждаю /
    ❌ Отменить»). The tap is an inbound message: it opens the window, so the
    offer-consent question that follows is free-form. Records and returns the
    delivery: text | template | failed.
    """
    req = get_request(req_id)
    if not req:
        return "failed"
    rows = _held_bookings(req["booking_ids"])
    if not rows:
        return "failed"
    delivery = client_notify.deliver(
        req["phone"], confirm_message(req.get("lang"), rows),
        "booking_confirm_request", [client_notify.fmt_slots(rows)],
        label="booking_confirm_request", lang=req.get("lang"), **_channel_kwargs(req))
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE consent_requests SET prompt_delivery = %s, updated_at = NOW() "
                            " WHERE id = %s", (delivery, req_id))
    except Exception:  # noqa: BLE001
        logger.exception("[CONSENT] prompt_delivery не сохранён для #%s", req_id)
    return delivery


def deliver_prompt_async(req_id: int) -> None:
    test_context.spawn_thread(deliver_prompt, req_id)


def _client_label(rows: list[dict], phone: str | None) -> str:
    name = next((r.get("customer_name") for r in rows if r.get("customer_name")), None) or "без имени"
    return f"{name}, {display_phone(phone or (rows[0].get('phone') if rows else ''))}"


def _booking_label(rows: list[dict]) -> str:
    ids = ", ".join(f"#{r['id']}" for r in rows)
    return f"{client_notify.fmt_slots(rows)} ({ids})"


def alert_admin_link(rows: list[dict], client_phone: str | None, payment_phone: str,
                     reason: str, amount, via_dialog: bool = False) -> str:
    """The admin's half of a link payment: what happened and what to do."""
    what = (f"Kaspi не найден ни на одном из номеров клиента "
            f"(спрошено {config.KASPI_PHONE_MAX_RETRIES} раз)"
            if reason == "no_kaspi" and via_dialog else
            "на номере для оплаты нет Kaspi"
            if reason == "no_kaspi" else
            "проверка номера в ApiPay не удалась")
    client = (f"{_client_label(rows, client_phone)}, "
              f"номер для оплаты {display_phone(payment_phone)}")
    details = (f"Аванс к оплате по ссылке: {_fmt_amount(amount)}. "
               f"Бронь снимется через {config.LINK_PAYMENT_TTL_SECONDS // 60} мин без "
               f"подтверждения. Счёт Kaspi на другой номер: "
               f"POST /api/manager/bookings/{rows[0]['id']}/invoice.")
    return client_notify.alert_admin(what, client, _booking_label(rows), details)


# ---------------------------------------------------------------------------
# Billing — the only ways out of awaiting_consent into payment
# ---------------------------------------------------------------------------

def issue_invoice(req_id: int, kaspi_phone: str) -> dict:
    """Bill `kaspi_phone` for request `req_id`: its bookings go to
    awaiting_payment and the invoice is queued in one transaction, then sent.

    Works on bookings still held for consent, and on ones that fell back to a
    payment link (an admin re-billing a corrected number). The number must
    already have been checked in Kaspi. Returns {ok, code, invoice?, bookings?,
    message}. A failed send takes the bookings back (no invoice → no booking).
    """
    from integrations import apipay_service

    normalized = apipay_client.normalize_phone(kaspi_phone)
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM consent_requests WHERE id = %s FOR UPDATE", (req_id,))
            req = cur.fetchone()
            if not req or req["step"] not in BILLABLE_STEPS:
                return {"ok": False, "code": "CONSENT_CLOSED",
                        "message": "Запрос согласия уже закрыт."}
            cur.execute(
                "SELECT id, state, consent_at, group_transition, reserved_until, payment_method "
                "  FROM bookings WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                (req["booking_ids"],))
            held = _billable([dict(r) for r in cur.fetchall()])
            if not held:
                cur.execute("UPDATE consent_requests SET step = 'cancelled', updated_at = NOW() "
                            " WHERE id = %s", (req_id,))
                return {"ok": False, "code": "BOOKINGS_GONE",
                        "message": "Брони больше не удерживаются."}
            if any(r["consent_at"] is None for r in held):
                return {"ok": False, "code": "CONSENT_REQUIRED",
                        "message": "Клиент ещё не согласился с офертой."}

            held_ids = [r["id"] for r in held]
            chargeable = [r for r in held if r["id"] in set(req["chargeable_ids"])]
            cur.execute(
                "UPDATE bookings SET state = 'awaiting_payment', kaspi_phone = %s, "
                "       payment_method = 'kaspi_invoice', "
                "       reserved_until = CASE WHEN reserved_until IS NULL THEN NULL "
                "            ELSE NOW() + make_interval(secs => %s) END, "
                "       updated_at = NOW() "
                " WHERE id = ANY(%s)",
                (normalized, config.PAYMENT_TTL_SECONDS, held_ids))
            for r in held:
                if r["state"] != "awaiting_payment":
                    _record_history(cur, r["id"], _HISTORY_SOURCE, key="status_change",
                                    old_status=_STATES_RUSSIAN.get(r["state"]),
                                    new_status=_STATES_RUSSIAN.get("awaiting_payment"))

            queued = None
            if chargeable:
                count = _slot_count(chargeable)
                queued = apipay_service.queue_invoice(
                    cur, normalized, [r["id"] for r in chargeable], count,
                    req.get("description") or apipay_service._avans_description(count),
                    source=req["source"], notify_chat_id=req.get("chat_id"),
                    notify_provider=req.get("provider"), notify_lang=req.get("lang"),
                    prepayment=_amount_for(req, count))
            cur.execute("UPDATE consent_requests SET step = 'done', updated_at = NOW() "
                        " WHERE id = %s", (req_id,))

    bookings = _bookings(held_ids)
    if queued is None:
        return {"ok": True, "code": "NO_INVOICE", "invoice": None, "bookings": bookings,
                "message": "Платных слотов нет — счёт не нужен."}
    try:
        sent = apipay_service.send_invoice(queued)
    except ApiPayError as exc:
        apipay_service.rollback_failed_send(queued, str(exc), booking_ids=held_ids)
        logger.error("[CONSENT] Счёт по запросу #%s не выставлен, брони %s сняты: %s",
                     req_id, held_ids, exc)
        return {"ok": False, "code": "PAYMENT_PROVIDER_ERROR", "error": str(exc),
                "bookings": bookings, "message": f"Не удалось выставить аванс: {exc}"}
    remember_kaspi_phone(req["phone"], normalized,
                         name=next((b.get("customer_name") for b in bookings
                                    if b.get("customer_name")), None))
    return {"ok": True, "code": "OK", "invoice": sent, "bookings": bookings,
            "message": "Счёт выставлен."}


def switch_to_link(req_id: int, payment_phone: str, reason: str) -> dict:
    """The payment number cannot be billed in Kaspi: hold the bookings for a
    payment by link instead (awaiting_payment, payment_method='link', held for
    LINK_PAYMENT_TTL_SECONDS) and alert the admin. The CLIENT message is the
    caller's — it differs between the chat (free-form) and the website.

    Returns {ok, bookings, amount}.
    """
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM consent_requests WHERE id = %s FOR UPDATE", (req_id,))
            req = cur.fetchone()
            if not req or req["step"] not in ACTIVE_STEPS:
                return {"ok": False, "bookings": []}
            cur.execute("SELECT id, state, group_transition FROM bookings "
                        " WHERE id = ANY(%s) AND state = 'awaiting_consent' FOR UPDATE",
                        (req["booking_ids"],))
            held = [dict(r) for r in cur.fetchall()]
            ids = [r["id"] for r in held]
            mark_link(cur, ids)
            for bid in ids:
                _record_history(cur, bid, _HISTORY_SOURCE, key="status_change",
                                old_status=_STATES_RUSSIAN.get("awaiting_consent"),
                                new_status=_STATES_RUSSIAN.get("awaiting_payment"))
            cur.execute("UPDATE consent_requests SET step = 'link', kaspi_phone = %s, "
                        "       updated_at = NOW() WHERE id = %s",
                        (payment_phone, req_id))
    rows = _bookings(ids)
    chargeable = [r for r in held if r["id"] in set(req["chargeable_ids"])]
    amount = _amount_for(req, _slot_count(chargeable) if chargeable else 0)
    if rows:
        alert_admin_link(rows, req["phone"], payment_phone, reason, amount, via_dialog=True)
    logger.warning("[CONSENT] Запрос #%s: оплата по ссылке (%s, номер %s), брони %s",
                   req_id, reason, payment_phone, ids)
    return {"ok": bool(rows), "bookings": rows, "amount": amount}


def mark_link(cur, booking_ids: list[int]) -> None:
    """Bookings to be paid by link: awaiting_payment, held LINK_PAYMENT_TTL_SECONDS."""
    cur.execute(
        "UPDATE bookings SET state = 'awaiting_payment', payment_method = 'link', "
        "       reserved_until = CASE WHEN reserved_until IS NULL THEN NULL "
        "            ELSE NOW() + make_interval(secs => %s) END, "
        "       updated_at = NOW() "
        " WHERE id = ANY(%s)",
        (config.LINK_PAYMENT_TTL_SECONDS, list(booking_ids)))
    for bid in booking_ids:
        cur.execute(
            "INSERT INTO booking_events (booking_id, event, actor_type, note) "
            "VALUES (%s, 'payment_link_sent', 'system', %s)", (bid, config.KASPI_PAYMENT_URL))


def invoice_sent_message(req: dict, result: dict) -> str:
    invoice = result["invoice"]
    return _t("invoice_sent", req.get("lang"), avans=_fmt_amount(invoice["amount"]),
              phone=display_phone(invoice["phone"]),
              slots=client_notify.fmt_slots(result.get("bookings") or []),
              ttl=config.PAYMENT_TTL_SECONDS // 60)


def notify_invoice_sent_async(req: dict, result: dict) -> None:
    """Tell the client their invoice is out (admin-issued path), off-thread."""
    invoice = result["invoice"]
    test_context.spawn_thread(
        client_notify.send, req["phone"], "booking_created_invoice", req.get("lang"),
        amount=_fmt_amount(invoice["amount"]), kaspi_phone=display_phone(invoice["phone"]),
        slots=client_notify.fmt_slots(result.get("bookings") or []),
        ttl=config.PAYMENT_TTL_SECONDS // 60, **_channel_kwargs(req))


# ---------------------------------------------------------------------------
# The dialog
# ---------------------------------------------------------------------------

def _refresh_sheets() -> None:
    try:
        from integrations.sheets.booking_sheets import refresh_all_bookings, refresh_week_sheet
        refresh_all_bookings()
        refresh_week_sheet()
    except Exception:  # noqa: BLE001
        logger.exception("[CONSENT] Обновление таблицы не удалось")


def _cancel_bookings(req: dict, reason: str, actor: str = "chatbot:Бот") -> list[dict]:
    """Cancel the request's bookings still held for consent; returns those cancelled."""
    from integrations.repo import postgres
    cancelled = []
    for b in _held_bookings(req["booking_ids"]):
        res = postgres.cancel_booking_trial("dopsy_bot", b["id"], actor_type=actor, reason=reason)
        if ((res or {}).get("data") or {}).get("cancelled"):
            cancelled.append(b)
    return cancelled


def _try_number(req: dict, number: str) -> str:
    """Bill `number` (Kaspi invoice) — or fall back to the payment link. Returns the reply."""
    lang = req.get("lang")
    from integrations import apipay_service
    reason = None
    try:
        apipay_service.ensure_kaspi_client(number)
    except KaspiClientMissing:
        reason = "no_kaspi"
    except ApiPayError as exc:
        logger.error("[CONSENT] Проверка номера %s в Kaspi не удалась: %s", number, exc)
        reason = "check_failed"

    if reason == "no_kaspi" and req["kaspi_retries"] < config.KASPI_PHONE_MAX_RETRIES:
        # Ask for another number; the slot stays held meanwhile.
        _set_step(req["id"], "new_number", kaspi_retries=req["kaspi_retries"] + 1)
        return _t("no_kaspi_ask", lang)
    if reason:
        res = switch_to_link(req["id"], number, reason)
        _refresh_sheets()
        if not res["ok"]:
            return _t("gone", lang)
        return _t("pay_by_link" if reason == "no_kaspi" else "check_failed_link", lang,
                  phone=display_phone(number),
                  slots=client_notify.fmt_slots(res["bookings"]),
                  avans=_fmt_amount(res["amount"]), link=config.KASPI_PAYMENT_URL)

    result = issue_invoice(req["id"], number)
    _refresh_sheets()
    if result["ok"] and result.get("invoice"):
        from chat.conversation import clear_history
        if req.get("chat_id"):
            clear_history(req["chat_id"])
        return invoice_sent_message(req, result)
    if result["ok"]:
        return _t("no_invoice", lang)
    if result["code"] == "PAYMENT_PROVIDER_ERROR":
        return _t("send_failed", lang, error=result.get("error", ""))
    return _t("gone", lang)


def _ask_same_number(req: dict, suggested: str) -> str:
    lang = req.get("lang")
    return _buttons(_t("same_number", lang, phone=display_phone(suggested)),
                    [_t("btn_same_yes", lang), _t("btn_same_no", lang)])


def handle_message(phone: str, text: str) -> str | None:
    """Route a client message into their open consent dialog.

    Returns the reply, or None when the client has no dialog in progress (the
    caller then handles the message as usual). While a dialog is open every
    message is an answer to it: anything unrecognised re-asks the question.
    """
    req = get_active_request(phone)
    if not req:
        return None
    if not _held_bookings(req["booking_ids"]):
        # A manager cancelled or confirmed them meanwhile — nothing to ask about.
        _set_step(req["id"], "cancelled")
        return None

    lang = req.get("lang")
    step = req["step"]
    logger.info("[CONSENT] #%s step=%s ответ=%.60r", req["id"], step, text)

    if step == "confirm":
        answer = parse_confirm(text)
        if answer == "no":
            if _set_step(req["id"], "declined", from_steps=("confirm",)):
                _cancel_bookings(req, REASON_BOOKING_DECLINED)
                _refresh_sheets()
            return _t("booking_declined", lang)
        if answer != "yes":
            return confirm_message(lang, _held_bookings(req["booking_ids"]))
        # Booking confirmed → now the offer (free-form: the tap opened the window).
        _set_step(req["id"], "offer", from_steps=("confirm",))
        return prompt_message(lang, _amount_for(req))

    if step == "offer":
        answer = parse_answer(text)
        if answer == "no":
            if _set_step(req["id"], "declined", from_steps=("offer",)):
                _cancel_bookings(req, REASON_DECLINED)
                _refresh_sheets()
                if req.get("chat_id"):
                    from chat.conversation import clear_history
                    clear_history(req["chat_id"])
            return _t("declined", lang)
        if answer != "yes":
            return prompt_message(lang, _amount_for(req))

        with _conn() as conn:
            with conn.cursor() as cur:
                record_consent(cur, req["booking_ids"], req["consent_channel"])
        if req.get("kaspi_phone"):
            # The manager entered the payment number with the booking.
            return _try_number(req, req["kaspi_phone"])
        suggested = parse_phone(last_kaspi_phone(req["phone"]) or "")
        own = parse_phone(req["phone"])
        if suggested and suggested != own:
            _set_step(req["id"], "same_number", from_steps=("offer",),
                      suggested_phone=suggested)
            return _ask_same_number(req, suggested)
        if own is None:
            # A foreign WhatsApp number can never be billed in Kaspi: ask.
            _set_step(req["id"], "new_number", from_steps=("offer",))
            return _t("ask_number", lang)
        return _try_number(req, own)

    if step == "same_number":
        answer = parse_answer(text, same_number=True)
        if answer == "yes":
            return _try_number(req, req["suggested_phone"])
        if answer == "no":
            _set_step(req["id"], "new_number", from_steps=("same_number",))
            return _t("ask_number", lang)
        typed = parse_phone(text)
        if typed:
            return _try_number(req, typed)
        return _ask_same_number(req, req["suggested_phone"])

    # step == "new_number"
    typed = parse_phone(text)
    if not typed:
        return _t("bad_format", lang)
    return _try_number(req, typed)


def cancel_for_phone(phone: str, reason: str = "user_reset") -> bool:
    """Close the client's open dialog and release its bookings (reset command)."""
    req = get_active_request(phone)
    if not req or not _set_step(req["id"], "cancelled"):
        return False
    _cancel_bookings(req, reason)
    return True


def expire_stale() -> int:
    """Release bookings whose consent window ran out. Run by the TTL sweeper.

    The client is told with the `booking_cancelled` template when they have not
    written in 24h — the end of the template-only flow for a client who never
    answered the confirm request.
    """
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "UPDATE consent_requests SET step = 'expired', updated_at = NOW() "
                " WHERE id IN (SELECT id FROM consent_requests "
                "               WHERE step = ANY(%s) AND expires_at < NOW() "
                "               FOR UPDATE SKIP LOCKED) "
                "RETURNING *", (list(ACTIVE_STEPS),))
            expired = [dict(r) for r in cur.fetchall()]
    released = 0
    for req in expired:
        cancelled = _cancel_bookings(req, REASON_EXPIRED, actor="system")
        released += len(cancelled)
        if cancelled:
            client_notify.send(req["phone"], "consent_expired", req.get("lang"),
                               slots=client_notify.fmt_slots(cancelled), **_channel_kwargs(req))
    if expired:
        logger.info("[CONSENT] Истекло запросов согласия: %d, снято броней: %d",
                    len(expired), released)
        if released:
            _refresh_sheets()
    return released
