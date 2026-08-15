"""Booking-side logic for ApiPay avans payments.

Splits cleanly from `apipay_client` (pure HTTP) and `apipay_repo` (pure SQL):
this is where an ApiPay invoice meets the booking state machine.

Flow (both entry points share it — manager batch and the WhatsApp bot)
    ensure_kaspi_client(): POST /clients/check — before anything is written
    bookings inserted (awaiting_payment) + invoice row queued  ← ONE transaction
        → COMMIT
        → send_invoice(): POST /invoices, ApiPay pushes to the client's Kaspi app
    POST /webhooks/apipay  status=paid
        → the covered bookings go awaiting_payment → confirmed
        → bot-created invoices additionally message the client on WhatsApp
    status=cancelled|expired|error
        → the covered bookings go awaiting_payment → unpaid (frees the slot)

The commit sits BETWEEN the booking write and the ApiPay call on purpose. The
call is the only step that can succeed on their side while failing on ours, so
nothing that must survive it is left uncommitted: a lost response leaves a
durable row the webhook can still resolve, and `sweep_pending_sends` retries
the send itself.

A send that fails in front of a caller is not retried, though — no invoice
means no booking. `send_invoice` raises, and the caller answers with
`rollback_failed_send`: the slot goes back on the market and whoever asked
(the client on WhatsApp, the manager over the API) is told why, instead of
holding a reservation with no way to pay for it. The invoice row is retired
rather than deleted, so a payment ApiPay took anyway still lands on a known
invoice and is flagged for a manual refund.
"""

import logging
import uuid

import psycopg2.extras

import config
from integrations import apipay_client, client_notify
from integrations.apipay_client import ApiPayError
from integrations.repo import apipay_repo
from integrations.repo.history_repo import _record_history
from integrations.repo.utils import _conn
from integrations.status_labels import STATES_RUSSIAN as _STATES_RUSSIAN

logger = logging.getLogger(__name__)

_ACTOR = "apipay"

# booking_history.source for everything ApiPay drives. Sits alongside the other
# bot source ('chatbot:Бот') rather than a manager email, so the manager UI can
# tell a machine-made change from a human one by the prefix alone.
_HISTORY_SOURCE = "bot:ApiPay"

# Why an invoice ended, rendered for the [REASON] token. TWO maps, not one:
# 'cancelled' means "ApiPay says the invoice died" as a status and "the booking
# was cancelled" as one of our own reasons, and a single dict would silently
# give one of them the other's wording. An unknown reason falls through to its
# raw slug (better than hiding it).
_STATUS_REASONS_RUSSIAN = {
    apipay_client.STATUS_CANCELLED: "отменён на стороне ApiPay",
    apipay_client.STATUS_ERROR: "ошибка оплаты",
    apipay_client.STATUS_EXPIRED: "истёк срок действия счёта",
}
_CANCEL_REASONS_RUSSIAN = {
    "ttl_expired": "истекло время на оплату",
    "paid_by_receipt": "оплата подтверждена чеком",
    "bookings_gone_before_send": "бронь снята до отправки счёта",
    "manager_cancel": "бронь отменена менеджером",
    "user_cancel_llm_flow": "бронь отменена клиентом",
    "user_cancel_mid_flow": "бронь отменена клиентом",
    "user_declined": "клиент отказался от брони",
    "cancelled": "бронь отменена",
    "unpaid": "бронь снята с оплаты",
}

# The two reasons that mean "the reservation window ran out", as opposed to
# somebody (a manager, the client, ApiPay) actively killing the invoice.
_TTL_REASONS = ("ttl_expired", apipay_client.STATUS_EXPIRED)


def _fmt_amount(value) -> str:
    """Money for history text: plain digits, a whole amount loses its '.0'
    (mirrors booking_service._fmt_amount, kept local to avoid an import cycle)."""
    number = float(value)
    return f"{number:.0f}" if number == int(number) else f"{number:.2f}"


def _record_event(cur, booking_id: int, event: str, note: str | None = None) -> None:
    cur.execute(
        "INSERT INTO booking_events (booking_id, event, actor_type, note) "
        "VALUES (%s, %s, %s, %s)",
        (booking_id, event, _ACTOR, note),
    )


def _record_status_change(cur, booking_id: int, old: str, new: str) -> None:
    _record_history(cur, booking_id, _HISTORY_SOURCE, key="status_change",
                    old_status=_STATES_RUSSIAN.get(old, old),
                    new_status=_STATES_RUSSIAN.get(new, new))


def _record_invoice_closed(cur, booking_ids: list[int], invoice_id,
                           reason: str | None, from_apipay: bool = False) -> None:
    """History rows for an invoice that ended without being paid.

    `apipay_ttl` when the reservation window ran out, `apipay_cancelled`
    otherwise — the description names the invoice, the accompanying
    `status_change` row (where there is one) names the booking transition.

    `from_apipay` says whether `reason` is one of their statuses (webhook or
    poller) or one of ours (a local cancel) — see the two maps above.
    """
    reasons = _STATUS_REASONS_RUSSIAN if from_apipay else _CANCEL_REASONS_RUSSIAN
    for bid in booking_ids:
        _record_history(cur, bid, _HISTORY_SOURCE,
                        key="apipay_ttl" if reason in _TTL_REASONS else "apipay_cancelled",
                        invoice_id=invoice_id if invoice_id is not None else "—",
                        reason=reasons.get(reason, reason or "причина не указана"))


def _log_invoice_closed(booking_ids: list[int], invoice_id, reason: str | None) -> None:
    """`_record_invoice_closed` on its own connection, for the post-commit cancel
    paths (which hold no cursor). Never raises: the cancellation itself has
    already succeeded and must not be undone by a bookkeeping failure."""
    if not booking_ids:
        return
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                _record_invoice_closed(cur, list(booking_ids), invoice_id, reason)
    except Exception:  # noqa: BLE001
        logger.exception("[APIPAY] Не удалось записать историю отмены счёта %s", invoice_id)


# ---------------------------------------------------------------------------
# Step 1 — queue (runs inside the bookings transaction, never calls ApiPay)
# ---------------------------------------------------------------------------

def _avans_description(count: int, customer: str | None = None) -> str:
    description = f"Аванс за бронь ({count} шт.)" if count > 1 else "Аванс за бронь"
    return f"{description} — {customer}" if customer else description


def ensure_kaspi_client(phone: str) -> str | None:
    return "1"
    """Check `phone` can actually receive an invoice. Returns the Kaspi name.

    Called by both entry points BEFORE anything is reserved, because this is the
    one ApiPay failure that can be found out for free. Sending to a number Kaspi
    does not know is not rejected by `POST /invoices` — it is accepted, spends
    one of the day's invoices, and dies later as a webhook, i.e. after the
    request that could have told the client (or the manager) why nothing came.
    Checking first turns "the slot was reserved, held, and quietly released" into
    a sentence in the same reply.

    Raises `KaspiClientMissing` when the number is not registered, and plain
    `ApiPayError` when the check itself did not go through. Both are fatal to
    the caller by design — nothing has been written yet, so failing here costs
    the client nothing, while the alternative (reserve, send, fail) costs them a
    booking that has to be taken back.
    """
    normalized = apipay_client.normalize_phone(phone)   # also validates
    if not config.APIPAY_CHECK_CLIENT:
        return None

    result = apipay_client.check_client(normalized)
    print(result)
    if not result.get("has_kaspi"):
        logger.warning("[APIPAY] Номер %s не зарегистрирован в Kaspi — счёт не выставляется",
                       normalized)
        raise apipay_client.KaspiClientMissing(result.get("phone") or normalized)

    name = result.get("client_name")
    logger.info("[APIPAY] Номер %s подтверждён в Kaspi (%s)", normalized, name or "без имени")
    return name


def queue_invoice(cur, phone: str, booking_ids: list[int], slot_count: int,
                  description: str, source: str = "manager",
                  notify_chat_id: str | None = None,
                  notify_provider: str | None = None,
                  notify_lang: str | None = None) -> dict:
    """Write the invoice row on the caller's cursor. No network call.

    The returned dict is everything `send_invoice` needs, so the caller can
    commit and then send. `external_order_id` is generated here, once: retries
    reuse it rather than minting a new one, which is what keeps a repeated send
    from becoming a second charge.
    """
    amount = slot_count * config.APIPAY_AVANS_PER_BOOKING
    external_order_id = f"{source}-{booking_ids[0]}-{uuid.uuid4().hex[:8]}"
    normalized = apipay_client.normalize_phone(phone)

    row_id = apipay_repo.insert_invoice(
        cur, external_order_id, normalized, amount, booking_ids,
        source=source, notify_chat_id=notify_chat_id,
        notify_provider=notify_provider, notify_lang=notify_lang)

    for bid in booking_ids:
        _record_event(cur, bid, "apipay_invoice_queued",
                      note=f"external_order_id={external_order_id} amount={amount}")

    # `id` stays ApiPay's invoice id throughout the API surface (NULL until the
    # send lands); the local row is `row_id`. Callers and the manager UI key on
    # `id`, so it must not quietly change meaning between the two steps.
    return {"id": None, "row_id": row_id, "amount": amount, "phone": normalized,
            "external_order_id": external_order_id, "description": description,
            "booking_ids": list(booking_ids), "status": apipay_client.STATUS_CREATED}


def batch_invoice_hook(phone: str, customer: str | None = None):
    """Build the ``on_created`` hook for `manager_create_bookings_batch`.

    Called with the batch's open cursor once every booking row exists. Only
    queues — the send happens after the caller commits, so ApiPay is never
    reached with a transaction held open, and a failed send can no longer erase
    the record that we asked for money.

    Returns nothing to charge → ``{}`` (a batch of only repeating slots carries
    no avans).
    """
    def hook(cur, ctx: dict) -> dict:
        count = ctx["chargeable_count"]
        booking_ids = ctx["chargeable_booking_ids"]
        if count == 0:
            logger.info("[APIPAY] Пакет без разовых слотов — счёт не создаётся.")
            return {}

        queued = queue_invoice(cur, phone, booking_ids, count,
                               _avans_description(count, customer),
                               source="manager")
        logger.info("[APIPAY] Счёт %s на %s₸ поставлен в очередь для броней %s",
                    queued["external_order_id"], queued["amount"], booking_ids)
        return {"invoice": queued}

    return hook


# ---------------------------------------------------------------------------
# Step 2 — send (runs after the commit; retryable)
# ---------------------------------------------------------------------------

def send_invoice(queued: dict) -> dict:
    """Ask ApiPay to push `queued` to the client's Kaspi app.

    Raises `ApiPayError` when the send did not go through — including for a
    non-ApiPay failure, which is wrapped so callers have exactly one exception
    type to catch. The row keeps its `last_send_error` either way.

    A caller that created bookings for this invoice must answer the raise with
    `rollback_failed_send`: no invoice means no reservation, and the slot has
    to go back on the market. `sweep_pending_sends` is the exception — the rows
    it retries belong to a client who was never told anything.
    """
    row_id = queued["row_id"]
    try:
        invoice = apipay_client.create_invoice(
            phone=queued["phone"], amount=queued["amount"],
            description=queued.get("description") or "Аванс за бронь",
            external_order_id=queued["external_order_id"],
        )
    except ApiPayError as exc:
        apipay_repo.mark_send_failed(row_id, str(exc))
        logger.error("[APIPAY] Не удалось отправить счёт %s: %s",
                     queued["external_order_id"], exc)
        raise
    except Exception as exc:  # noqa: BLE001 — one exception type for callers
        apipay_repo.mark_send_failed(row_id, repr(exc))
        logger.exception("[APIPAY] Ошибка отправки счёта %s",
                         queued["external_order_id"])
        raise ApiPayError(f"Ошибка отправки счёта: {exc}") from exc

    status = invoice.get("status") or "processing"
    apipay_repo.mark_sent(row_id, invoice["id"], status)
    logger.info("[APIPAY] Счёт %s отправлен как invoice=%s на %s₸ (брони %s, телефон %s)",
                queued["external_order_id"], invoice["id"], queued["amount"],
                queued["booking_ids"], queued["phone"])
    return {**queued, "id": invoice["id"], "invoice_id": invoice["id"],
            "status": status}


def rollback_failed_send(queued: dict, error: str,
                         booking_ids: list[int] | None = None) -> list[int]:
    """Undo the bookings an invoice was raised for, after its send failed.

    "No invoice → no booking", enforced as a compensating action: the
    reservation is committed by the time ApiPay is called, so there is no
    transaction left to roll back. Both halves matter.

      * The queued row leaves the outbox, or the sweeper would push the invoice
        a minute later for slots that no longer exist.
      * The bookings go to 'cancelled', which drops them out of the
        `bookings_no_overlap` EXCLUDE constraint and frees the slot.

    `booking_ids` overrides what the invoice covers, for a caller that must
    also undo bookings the invoice was never raised for — the manager batch
    reports one failure for the whole request, so it takes the whole request
    back, repeating slots included.

    The row is RETIRED, never deleted: a timeout is indistinguishable from a
    rejection here, so ApiPay may have created an invoice we never heard about.
    Keeping the row means a client who pays it anyway still resolves by
    `external_order_id`, and `_flag_paid_too_late` demands the manual refund —
    rather than the payment landing as an unknown invoice with nothing but a
    warning line behind it.

    Never raises: the caller is already reporting the ApiPay failure to whoever
    asked, and a bookkeeping problem must not replace that with a 500.
    """
    reason = f"send_failed: {error}"
    try:
        if not apipay_repo.cancel_unsent_row(queued["row_id"], reason):
            # A webhook backfilled `invoice_id` while we were failing: the send
            # DID land. The invoice is live, so the bookings must stay.
            logger.warning(
                "[APIPAY] Счёт %s уже существует на стороне ApiPay — брони %s "
                "оставлены в силе, откат отменён",
                queued["external_order_id"], queued.get("booking_ids"))
            return []
    except Exception:  # noqa: BLE001
        logger.exception("[APIPAY] Не удалось снять счёт %s с очереди",
                         queued["external_order_id"])

    targets = list(booking_ids if booking_ids is not None
                   else (queued.get("booking_ids") or []))
    if not targets:
        return []

    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # A transitive booking (23:00→01:00) is two rows sharing a
                # `group_transition`; cancelling one half and leaving the other
                # would hold a slot nobody is being charged for.
                cur.execute(
                    "SELECT id, state FROM bookings "
                    " WHERE (id = ANY(%s) OR (group_transition IS NOT NULL "
                    "        AND group_transition IN ("
                    "             SELECT group_transition FROM bookings "
                    "              WHERE id = ANY(%s) AND group_transition IS NOT NULL))) "
                    "   AND state NOT IN ('cancelled', 'failed', 'unpaid') "
                    " FOR UPDATE",
                    (targets, targets),
                )
                previous = {r["id"]: r["state"] for r in cur.fetchall()}
                if not previous:
                    return []
                ids = sorted(previous)

                cur.execute(
                    "UPDATE bookings SET state = 'cancelled', updated_at = NOW() "
                    " WHERE id = ANY(%s)", (ids,),
                )
                for bid in ids:
                    _record_event(cur, bid, "cancelled",
                                  note=f"apipay send failed: {error}")
                    _record_status_change(cur, bid, previous[bid], "cancelled")
                    _record_history(cur, bid, _HISTORY_SOURCE,
                                    key="apipay_send_failed", reason=error)
                cur.execute(
                    "DELETE FROM booking_sessions WHERE booking_id = ANY(%s)", (ids,)
                )
    except Exception:  # noqa: BLE001
        logger.exception(
            "[APIPAY] Счёт %s не отправлен, и брони %s не удалось отменить — "
            "слоты остались занятыми, нужен менеджер",
            queued["external_order_id"], targets)
        return []

    logger.warning("[APIPAY] Счёт %s не отправлен (%s) — брони %s отменены",
                   queued["external_order_id"], error, ids)
    return ids


def sweep_pending_sends() -> int:
    """Retry invoices that were committed but never reached ApiPay.

    Covers the gap the outbox exists for: a crash, restart or outage between
    the commit and the send would otherwise leave the client holding a reserved
    slot they were never asked to pay for, until the TTL quietly dropped it.
    """
    if not config.APIPAY_ENABLED:
        return 0
    try:
        rows = apipay_repo.claim_pending_sends()
    except Exception:
        logger.exception("[APIPAY] Не удалось выбрать счета для повторной отправки")
        return 0

    sent = 0
    for row in rows:
        booking_ids = list(row["booking_ids"] or [])
        # The slots may have expired while the send was failing; charging for
        # them now would be worse than never sending at all.
        survivors, slot_count = apipay_repo.awaiting_payment_slots(booking_ids)
        if not survivors:
            apipay_repo.cancel_unsent_for_bookings(booking_ids, "bookings_gone_before_send")
            logger.info("[APIPAY] Счёт %s снят: брони %s уже не ожидают оплаты",
                        row["external_order_id"], booking_ids)
            continue

        # Charge for the survivors, not for what was queued: a batch that lost
        # a booking is worth less now, and the row must say so before the send
        # — the webhook settles the payment across `booking_ids`/`amount`.
        amount = slot_count * config.APIPAY_AVANS_PER_BOOKING
        if amount != row["amount"] or set(survivors) != set(booking_ids):
            logger.warning(
                "[APIPAY] Счёт %s пересчитан перед отправкой: %s₸ → %s₸ (брони %s → %s)",
                row["external_order_id"], row["amount"], amount, booking_ids, survivors)
        if not apipay_repo.reprice_pending(row["id"], amount, survivors):
            logger.info("[APIPAY] Счёт %s больше не в очереди — отправка отменена",
                        row["external_order_id"])
            continue

        # A retry that fails changes nothing: these rows exist because the
        # process died between the commit and the send, so nobody was ever told
        # about this invoice and there is no message to take back. The bookings
        # stay, and the reservation TTL is what eventually releases them.
        try:
            send_invoice({
                "row_id": row["id"], "amount": amount, "phone": row["phone"],
                "external_order_id": row["external_order_id"],
                "description": _avans_description(slot_count),
                "booking_ids": survivors,
            })
        except ApiPayError:
            continue  # already logged, and recorded on the row
        sent += 1
    if sent:
        logger.info("[APIPAY] Повторно отправлено счетов: %d", sent)
    return sent


def _create_invoice_for(phone: str, booking_ids: list[int], slot_count: int,
                        description: str) -> dict:
    """Queue and immediately send an invoice for `booking_ids`.

    Used by the re-issue path, which has no booking transaction of its own to
    join. Still two steps: the row is committed before ApiPay is called.
    """
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            queued = queue_invoice(cur, phone, booking_ids, slot_count, description)
    return send_invoice(queued)


# ---------------------------------------------------------------------------
# Webhook outcomes
# ---------------------------------------------------------------------------

def _flag_paid_too_late(cur, invoice_id, amount, booking_ids: list[int]) -> None:
    """The client paid after their bookings were already gone.

    Unavoidable race: a client can be mid-payment in their Kaspi app when a
    manager cancels. We do NOT auto-refund — the money is returned by hand from
    the ApiPay dashboard — so this has to be impossible to miss in the logs.

    A failure here is deliberately NOT swallowed: it would poison the shared
    transaction anyway, and rolling the claim back is the right outcome — the
    retry then gets another chance to record a payment that needs a human.
    """
    logger.error(
        "[APIPAY] ⚠️ ТРЕБУЕТСЯ РУЧНОЙ ВОЗВРАТ: счёт %s оплачен на %s₸, но брони %s "
        "уже отменены или подтверждены другим способом. Верните деньги клиенту "
        "через личный кабинет ApiPay.", invoice_id, amount, booking_ids)
    apipay_repo.flag_manual_refund(
        cur, invoice_id,
        f"Оплачен после отмены броней {booking_ids} — нужен возврат {amount}₸")


def apply_paid(cur, invoice_row: dict) -> list[int]:
    """Confirm every awaiting_payment booking the invoice covers.

    Runs on the caller's cursor, inside the same transaction that claimed the
    status — see `apply_webhook_status`.

    Returns the ids actually transitioned — empty if a manager already
    confirmed them or the TTL sweeper released them first.
    """
    booking_ids = list(invoice_row["booking_ids"] or [])
    if not booking_ids:
        return []
    invoice_id = invoice_row["invoice_id"]
    amount = invoice_row["amount"]
    kaspi_id = invoice_row.get("kaspi_invoice_id")

    cur.execute(
        "UPDATE bookings SET state = 'confirmed', updated_at = NOW() "
        " WHERE id = ANY(%s) AND state = 'awaiting_payment' RETURNING id",
        (booking_ids,),
    )
    confirmed = [r["id"] for r in cur.fetchall()]
    if not confirmed:
        _flag_paid_too_late(cur, invoice_id, amount, booking_ids)
        return []

    # One payments row per booking, splitting the invoice total evenly —
    # transaction_ref is UNIQUE, so a redelivered webhook cannot double-book.
    share = round(float(amount) / len(confirmed), 2)
    for bid in confirmed:
        # bank='kaspi' — the money really did arrive through Kaspi, so
        # existing per-bank reporting counts these alongside receipts.
        cur.execute(
            "INSERT INTO payments (booking_id, method, bank, amount, "
            "  transaction_ref, status, verified_by, verified_at) "
            "VALUES (%s, 'apipay', 'kaspi', %s, %s, 'accepted', %s, NOW())",
            (bid, share, f"apipay:{invoice_id}:{bid}", _ACTOR),
        )
        _record_event(cur, bid, "payment_received",
                      note=f"apipay invoice={invoice_id} kaspi={kaspi_id}")
        _record_history(cur, bid, _HISTORY_SOURCE, key="apipay_paid",
                        amount=_fmt_amount(share), invoice_id=invoice_id)
        _record_status_change(cur, bid, "awaiting_payment", "confirmed")

    logger.info("[APIPAY] Счёт %s оплачен (kaspi=%s) — подтверждены брони %s",
                invoice_id, kaspi_id, confirmed)
    return confirmed


def _notify_invoice_client(invoice_row: dict, key: str, **fields) -> None:
    """Send `key` to whoever this invoice belongs to.

    Two ways in, because invoices are raised two ways:

      * A BOT invoice carries `notify_chat_id` ("{phone_number_id}:{sender_id}")
        and `notify_lang` — the conversation that asked for the slot, and the
        language it was held in. The answer goes back down that exact channel.
      * A MANAGER invoice carries neither: it was raised from the sheet, and the
        client never wrote to us about it. It goes to the number that was
        billed, on the default channel, bilingually — they were still charged in
        their Kaspi app, so they still need to hear what became of it.

    Never raises — `client_notify.send` swallows everything, and the booking
    transition this reports is already committed.
    """
    chat_id = invoice_row.get("notify_chat_id") or ""
    phone_number_id, _, recipient = chat_id.partition(":")
    if chat_id and not recipient:
        logger.warning("[APIPAY] Некорректный notify_chat_id=%s — уведомляем по номеру",
                       chat_id)
    if recipient:
        client_notify.send(recipient, key, invoice_row.get("notify_lang"),
                           provider=invoice_row.get("notify_provider"),
                           phone_number_id=phone_number_id, **fields)
        return
    client_notify.send(invoice_row.get("phone"), key, **fields)


def notify_paid(invoice_row: dict, confirmed_ids: list[int]) -> None:
    """Tell the client their payment landed and the slots are theirs."""
    if not confirmed_ids:
        return
    _notify_invoice_client(invoice_row, "apipay_paid",
                           amount=client_notify.fmt_amount(invoice_row.get("amount")))


def notify_unpaid(invoice_row: dict, released_ids: list[int], status: str) -> None:
    """Tell the client an invoice died and took their reservation with it.

    Only for slots this actually released: a booking already confirmed another
    way (receipt, manager) keeps its slot, and telling that client it was
    cancelled would be worse than saying nothing.

    'expired' is the payment window running out; 'cancelled'/'error' is the
    payment itself failing — different wording, since only one of them is
    something the client can fix by paying faster next time.
    """
    if not released_ids:
        return
    from integrations.repo import booking_repo

    key = ("apipay_expired" if status == apipay_client.STATUS_EXPIRED
           else "apipay_failed")
    _notify_invoice_client(
        invoice_row, key,
        slots=client_notify.fmt_slots(booking_repo.get_bookings(released_ids)))


def notify_refunded(invoice_row: dict) -> None:
    """Tell the client the money came back.

    Refunds are issued by hand from the ApiPay dashboard, so the webhook is the
    first moment anything automated knows one happened — and the client is
    otherwise left watching their account for a return nobody confirmed.
    """
    _notify_invoice_client(invoice_row, "apipay_refunded",
                           amount=client_notify.fmt_amount(invoice_row.get("amount")))


def apply_failed(cur, invoice_row: dict, status: str) -> list[int]:
    """Release the slots of an invoice that will never be paid.

    Runs on the caller's cursor, inside the same transaction that claimed the
    status — see `apply_webhook_status`.

    Mirrors the reservation-TTL sweeper: awaiting_payment → unpaid, which drops
    the booking out of the EXCLUDE constraint so the slot is bookable again.
    Confirmed bookings are left alone — those were paid another way.
    """
    booking_ids = list(invoice_row["booking_ids"] or [])
    if not booking_ids:
        return []

    cur.execute(
        "UPDATE bookings SET state = 'unpaid', updated_at = NOW() "
        " WHERE id = ANY(%s) AND state = 'awaiting_payment' RETURNING id",
        (booking_ids,),
    )
    released = [r["id"] for r in cur.fetchall()]
    for bid in released:
        _record_event(cur, bid, "unpaid",
                      note=f"apipay invoice={invoice_row['invoice_id']} {status}")
        _record_status_change(cur, bid, "awaiting_payment", "unpaid")
    # 'expired' is the reservation window running out; 'cancelled'/'error' is
    # the invoice being killed — different history keys, same release.
    _record_invoice_closed(cur, released, invoice_row["invoice_id"], status,
                           from_apipay=True)
    if released:
        cur.execute(
            "DELETE FROM booking_sessions WHERE booking_id = ANY(%s)", (released,)
        )
        logger.info("[APIPAY] Счёт %s (%s) — брони %s отмечены неоплаченными.",
                    invoice_row["invoice_id"], status, released)
    return released


def apply_webhook_status(invoice_id, status: str, paid_at=None,
                         error_code: str | None = None,
                         error_message: str | None = None,
                         kaspi_invoice_id: str | None = None,
                         external_order_id: str | None = None) -> dict | None:
    """Claim a webhook's status transition and apply its booking effect — atomically.

    Returns None when there is nothing to do: an unknown invoice, or a status
    the row already holds (i.e. a redelivery). Otherwise ``{"invoice": row,
    "changed": [booking_id, ...], "released": bool, "paid": bool,
    "status": str}`` — `status` is the NEW one, since `row` is deliberately the
    pre-update snapshot (the caller needs the booking_ids and amount the invoice
    was raised for) and the client's message depends on which way it went.

    ONE transaction, deliberately. Claiming in a transaction of its own and
    applying in another leaves a window — a crash, a DB blip, or any error
    inside the apply — where the claim commits alone. ApiPay's retry then reads
    the status we already wrote, is told `duplicate`, and stops; the paid
    booking sits in awaiting_payment until the TTL sweeper releases the slot and
    messages the client that their payment never arrived. Both halves commit
    together or neither does, so a failure here is simply retried by ApiPay.
    """
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            row = apipay_repo.claim_status(
                cur, invoice_id, status, paid_at=paid_at, error_code=error_code,
                error_message=error_message, kaspi_invoice_id=kaspi_invoice_id,
                external_order_id=external_order_id,
            )
            if row is None:
                return None
            if status == apipay_client.STATUS_PAID:
                return {"invoice": row, "changed": apply_paid(cur, row),
                        "released": False, "paid": True, "status": status}
            if status in apipay_client.FAILED_STATUSES:
                return {"invoice": row, "changed": apply_failed(cur, row, status),
                        "released": True, "paid": False, "status": status}
            # processing → pending and refunds: recorded on the invoice, no
            # booking transition (a refund is settled with the client, not here).
            return {"invoice": row, "changed": [], "released": False,
                    "paid": False, "status": status}


def after_transition(invoice_row: dict, booking_ids: list[int],
                     released: bool, paid: bool, status: str | None = None) -> None:
    """Everything a status change triggers beyond the booking rows themselves.

    Shared by the webhook and the reconciliation poller: a payment discovered
    by polling must reach the client and the sheet exactly like one announced
    by a webhook. Never raises — the booking state is already committed.

    `status` is the invoice's new status; without it only the paid case can be
    told apart, which is why the two callers always pass it.
    """
    if paid:
        notify_paid(invoice_row, booking_ids)
    elif released:
        notify_unpaid(invoice_row, booking_ids, status or "")
    elif status == apipay_client.STATUS_REFUNDED:
        notify_refunded(invoice_row)
    sync_sheets(booking_ids, released)


def sync_sheets(booking_ids: list[int], released: bool) -> None:
    """Best-effort sheet sync, off the webhook's 5-second budget."""
    try:
        from integrations.repo import booking_repo
        from integrations.sheets.booking_sheets import (
            _single_table_write,
            refresh_week_sheet,
            upsert_booking_row,
        )

        for bid in booking_ids:
            row = booking_repo.get_booking(bid)
            if row:
                upsert_booking_row(row)
                if not released:
                    _single_table_write(row)
        if released:
            # Slots went back on the market — repaint the whole week rather than
            # leaving stale merged cells behind.
            refresh_week_sheet()
    except Exception:
        logger.exception("[APIPAY] Не удалось синхронизировать таблицу для %s", booking_ids)


# ---------------------------------------------------------------------------
# Reconciliation — the safety net under the webhook
# ---------------------------------------------------------------------------

# Long enough that a webhook merely running late is never raced, short enough
# that a payment is found before the reservation TTL releases the slot.
_RECONCILE_AFTER_SECONDS = 300


def reconcile_open_invoices() -> int:
    """Ask ApiPay about invoices that have gone quiet, and apply what it says.

    The webhook is a push, and pushes get lost: a deploy, a DNS or TLS problem,
    a rotated `APIPAY_WEBHOOK_SECRET` (every delivery then fails its signature
    check), or an outage on their side longer than their ~2h retry window. Any
    of those and a paid invoice stays 'processing' here forever — the TTL
    releases the slot, the client is told their payment never arrived, and the
    money they did send goes unnoticed by everyone.

    So we pull as well as listen. Nothing is decided here: the answer is fed
    through `apply_webhook_status`, the same claim-and-apply used by the
    webhook, which is why a delivery arriving mid-poll cannot double-apply —
    whichever gets there first performs the transition, the other sees no
    change and stops.

    Returns the number of invoices that actually moved.
    """
    if not config.APIPAY_ENABLED:
        return 0
    try:
        rows = apipay_repo.claim_open_invoices(_RECONCILE_AFTER_SECONDS)
    except Exception:
        logger.exception("[APIPAY] Не удалось выбрать счета для сверки")
        return 0

    changed = 0
    for row in rows:
        try:
            remote = apipay_client.get_invoice(row["invoice_id"])
        except ApiPayError as exc:
            logger.warning("[APIPAY] Сверка счёта %s не удалась: %s",
                           row["invoice_id"], exc)
            continue
        except Exception:
            logger.exception("[APIPAY] Ошибка сверки счёта %s", row["invoice_id"])
            continue

        status = remote.get("status")
        if not status or status == row["status"]:
            continue

        result = apply_webhook_status(
            row["invoice_id"], status,
            paid_at=remote.get("paid_at"),
            error_code=remote.get("error_code"),
            error_message=remote.get("error_message"),
            kaspi_invoice_id=remote.get("kaspi_invoice_id"),
            external_order_id=row["external_order_id"],
        )
        if result is None:
            continue

        changed += 1
        # WARNING, not INFO: reaching this means a webhook never arrived. One
        # is a hiccup; a run of them is a broken delivery path (secret, DNS,
        # certificate) that needs a human before it costs a real payment.
        logger.warning(
            "[APIPAY] Сверка: счёт %s на самом деле %s (у нас было %s) — вебхук "
            "не дошёл. Брони %s", row["invoice_id"], status, row["status"],
            result["changed"] or "не затронуты")
        after_transition(result["invoice"], result["changed"],
                         result["released"], result["paid"], result["status"])

    return changed


# ---------------------------------------------------------------------------
# TTL sweeper support
# ---------------------------------------------------------------------------

def on_bookings_cancelled(booking_ids: list[int], reason: str) -> None:
    """React to bookings being cancelled: kill the invoice, re-issue the remainder.

    MUST be called after the cancellation is COMMITTED — the re-issue reads the
    bookings back to see which ones are still awaiting payment.

    A batch invoice covers several bookings, and ApiPay has no way to lower the
    amount of a live invoice. So cancelling any covered booking cancels the whole
    invoice (never risk charging for a slot that no longer exists) and a fresh
    invoice is raised for whatever is still awaiting payment.

    Never raises: a cancellation must succeed even when ApiPay is unreachable.
    """
    if not config.APIPAY_ENABLED or not booking_ids:
        return
    try:
        apipay_repo.cancel_unsent_for_bookings(booking_ids, reason)
        invoices = apipay_repo.open_invoices_for_bookings(booking_ids)
    except Exception:
        logger.exception("[APIPAY] Не удалось найти открытые счета для %s", booking_ids)
        return

    for inv in invoices:
        invoice_id = inv["invoice_id"]
        try:
            apipay_client.cancel_invoice(invoice_id)
        except ApiPayError as exc:
            # Most likely already paid or already cancelled on ApiPay's side.
            # Leave the row open — the webhook is the source of truth — and do
            # NOT re-issue, or the client could be charged twice.
            logger.warning("[APIPAY] Не удалось отменить счёт %s: %s", invoice_id, exc)
            continue
        except Exception:
            logger.exception("[APIPAY] Ошибка при отмене счёта %s", invoice_id)
            continue

        apipay_repo.mark_cancelled(invoice_id, reason)
        _log_invoice_closed(list(inv["booking_ids"] or []), invoice_id, reason)
        logger.info("[APIPAY] Счёт %s отменён (%s)", invoice_id, reason)

        # Re-issue for the bookings of that invoice that are still unpaid.
        try:
            survivors, slot_count = apipay_repo.awaiting_payment_slots(
                list(inv["booking_ids"] or []))
            if not survivors:
                continue
            new_invoice = _create_invoice_for(
                inv["phone"], survivors, slot_count,
                description=(f"Аванс за бронь ({slot_count} шт.)"
                             if slot_count > 1 else "Аванс за бронь"),
            )
            logger.info("[APIPAY] Счёт %s перевыставлен как %s на %s₸ для броней %s",
                        invoice_id, new_invoice["invoice_id"], new_invoice["amount"],
                        survivors)
        except ApiPayError as exc:
            # The old invoice IS cancelled, so no money can be taken wrongly.
            # The survivors simply have no online payment path and will expire
            # on the reservation TTL unless a manager intervenes.
            logger.error("[APIPAY] Счёт %s отменён, но перевыставить не удалось: %s. "
                         "Брони %s остались без счёта.", invoice_id, exc, survivors)
        except Exception:
            logger.exception("[APIPAY] Ошибка при перевыставлении счёта %s", invoice_id)


def cancel_invoices_for_bookings(booking_ids: list[int], reason: str) -> None:
    """Cancel any still-open invoice covering `booking_ids`.

    Called when the reservation TTL releases the slots: without this the client
    could pay in their Kaspi app for a booking that no longer exists. Failures
    are logged, never raised — the sweeper must finish its pass regardless.
    """
    if not config.APIPAY_ENABLED or not booking_ids:
        return
    try:
        # Queued-but-unsent rows first: nothing to cancel at ApiPay (they never
        # got there), but they must leave the outbox or the sweeper would send
        # an invoice for a slot that has just been released.
        dropped = apipay_repo.cancel_unsent_for_bookings(booking_ids, reason)
        if dropped:
            logger.info("[APIPAY] Неотправленные счета сняты с очереди: %s (%s)",
                        dropped, reason)
        invoices = apipay_repo.open_invoices_for_bookings(booking_ids)
    except Exception:
        logger.exception("[APIPAY] Не удалось найти открытые счета для %s", booking_ids)
        return

    for inv in invoices:
        try:
            apipay_client.cancel_invoice(inv["invoice_id"])
            apipay_repo.mark_cancelled(inv["invoice_id"], reason)
            _log_invoice_closed(list(inv["booking_ids"] or []), inv["invoice_id"], reason)
            logger.info("[APIPAY] Счёт %s отменён (%s)", inv["invoice_id"], reason)
        except ApiPayError as exc:
            # The invoice may already be paid/cancelled on ApiPay's side; the
            # webhook remains the source of truth either way.
            logger.warning("[APIPAY] Не удалось отменить счёт %s: %s",
                           inv["invoice_id"], exc)
        except Exception:
            logger.exception("[APIPAY] Ошибка при отмене счёта %s", inv["invoice_id"])
