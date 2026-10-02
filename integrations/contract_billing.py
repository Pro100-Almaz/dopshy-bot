"""Subscription-style payment plans for contracts.

A contract's `price` is collected in INSTALLMENTS (table
`contract_installments`, migration 056). Each installment is asked for on its
due date by `issue_due_installments` (APScheduler, every minute):

    installment 'scheduled', due_at <= now
        → amount fixed, row 'issued'                      ← ONE transaction
          + apipay_invoices row queued (Kaspi number)
        → COMMIT
        → POST /invoices (ApiPay pushes it to the client's Kaspi app)
          or a WhatsApp message with the payment link (number not in Kaspi)
    POST /webhooks/apipay  status=paid        → installment 'paid'
                           expired/cancelled/error → 'scheduled' again after
                               CONTRACT_INVOICE_RETRY_HOURS, or 'overdue' once
                               CONTRACT_INVOICE_MAX_ATTEMPTS invoices went unpaid

Why our own scheduler and not ApiPay's /subscriptions: that API bills one fixed
amount on a fixed calendar. The per-booking mode needs the due date to follow
the bookings and the amount to follow what is left to pay, and the "not in
Kaspi" path needs a delivery ApiPay does not do. Invoices raised here go through
the SAME outbox, webhook, and reconciliation poller as the booking avans — an
installment invoice is an `apipay_invoices` row with `contract_installment_id`
set and `booking_ids` empty, so none of the booking-side logic touches it.

Two modes (`contracts.payment_mode`):

  static   N installments on fixed dates (explicit `due_dates`, or a
           `frequency` from `first_due_date`). Amounts are fixed when the plan
           is made — an even split with the remainder on the last one, or
           `amounts` given by the manager — and re-spread when the price
           changes or a manager locks one installment's amount.
  dynamic  One installment per booked slot, due when that slot ENDS. Its
           amount is worked out at issue time:

               (price - paid - invoiced but unpaid - manager-fixed amounts)
               ------------------------------------------------------------
                          installments still to be issued

           so adding or cancelling bookings re-spreads what is left, and the
           last installment always closes the price exactly.

Everything here that talks to ApiPay or WhatsApp does so AFTER its transaction
commits, and never raises into the caller's cancellation path.
"""

import calendar
import logging
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import psycopg2.extras

import config
from integrations import apipay_client, client_notify, test_context
from integrations.apipay_client import ApiPayError
from integrations.repo import apipay_repo
from integrations.repo.utils import _conn, _err, _ok

logger = logging.getLogger(__name__)

MODE_STATIC = "static"
MODE_DYNAMIC = "dynamic"
MODES = (MODE_STATIC, MODE_DYNAMIC)

FREQUENCIES = ("weekly", "biweekly", "monthly")

CHANNEL_KASPI = "kaspi_invoice"
CHANNEL_LINK = "whatsapp_link"
CHANNELS = (CHANNEL_KASPI, CHANNEL_LINK)

SCHEDULED = "scheduled"
ISSUED = "issued"
PAID = "paid"
OVERDUE = "overdue"
CANCELLED = "cancelled"
# Installments that still owe money once asked for — counted as "committed"
# when the next dynamic amount is worked out, so they are not re-spread.
_OWED = (ISSUED, OVERDUE)

_MAX_INSTALLMENTS = 120          # ten years of monthly payments
_DEAD_BOOKING_STATES = ("cancelled", "failed", "unpaid")


class PlanError(Exception):
    """A plan operation that must roll its transaction back with an answer."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Pure helpers — no DB, no network
# ---------------------------------------------------------------------------

def _tz() -> ZoneInfo:
    return ZoneInfo(config.BOOKING_TIMEZONE)


def _parse_date(value, name: str) -> date:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise PlanError("INVALID_PLAN", f"{name} должен быть датой в формате YYYY-MM-DD.")


def _whole_tenge(value, name: str) -> Decimal:
    """Money as whole tenge — ApiPay rejects kopecks on phone invoices."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise PlanError("INVALID_PLAN", f"{name} должен быть числом.")
    if amount <= 0 or amount != amount.to_integral_value():
        raise PlanError("INVALID_PLAN", f"{name} должен быть целым числом тенге больше нуля.")
    return amount


def _add_months(d: date, months: int) -> date:
    """Same day `months` later, clamped to the month's end (31 Jan → 28/29 Feb)."""
    month_index = d.month - 1 + months
    year, month = d.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _step(first: date, frequency: str, k: int) -> date:
    if frequency == "weekly":
        return first + timedelta(days=7 * k)
    if frequency == "biweekly":
        return first + timedelta(days=14 * k)
    return _add_months(first, k)


def due_at_for(day: date) -> datetime:
    """A static installment falls due on `day` at CONTRACT_BILLING_TIME, local time."""
    try:
        hh, mm = (int(x) for x in config.CONTRACT_BILLING_TIME.split(":"))
    except ValueError:
        hh, mm = 13, 0
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=_tz())


def split_evenly(total: Decimal, parts: int) -> list[Decimal]:
    """Whole-tenge split; the remainder goes on the LAST part so it closes exactly."""
    base = (total / parts).to_integral_value(rounding="ROUND_FLOOR")
    amounts = [base] * parts
    amounts[-1] += total - base * parts
    return amounts


def _static_schedule(plan: dict, total: Decimal, start_date, end_date) -> tuple[str | None, list[dict]]:
    """Due dates and amounts of a static plan, from `due_dates` or a frequency."""
    frequency = plan.get("frequency")
    if plan.get("due_dates"):
        raw = plan["due_dates"]
        if not isinstance(raw, list):
            raise PlanError("INVALID_PLAN", "due_dates должен быть списком дат.")
        days = sorted({_parse_date(d, "due_dates") for d in raw})
    else:
        if frequency not in FREQUENCIES:
            raise PlanError("INVALID_PLAN",
                            f"frequency должен быть одним из {FREQUENCIES} (или передайте due_dates).")
        first = _parse_date(plan.get("first_due_date") or start_date, "first_due_date")
        count = plan.get("installments")
        if count is not None:
            if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                raise PlanError("INVALID_PLAN", "installments должен быть целым числом ≥ 1.")
            days = [_step(first, frequency, k) for k in range(count)]
        else:
            # Unknown count: one per period until the contract ends.
            if not end_date:
                raise PlanError("INVALID_PLAN",
                                "Укажите installments или due_dates — у договора нет даты окончания.")
            last = _parse_date(end_date, "end_date")
            days = []
            while len(days) <= _MAX_INSTALLMENTS:
                day = _step(first, frequency, len(days))
                if day > last:
                    break
                days.append(day)
            if not days:
                raise PlanError("INVALID_PLAN", "first_due_date позже окончания договора.")
    if not days:
        raise PlanError("INVALID_PLAN", "В плане нет ни одного платежа.")
    if len(days) > _MAX_INSTALLMENTS:
        raise PlanError("INVALID_PLAN", f"Слишком много платежей (максимум {_MAX_INSTALLMENTS}).")

    amounts = plan.get("amounts")
    if amounts is not None:
        if not isinstance(amounts, list) or len(amounts) != len(days):
            raise PlanError("INVALID_PLAN", "amounts должен содержать по сумме на каждый платёж.")
        fixed = [_whole_tenge(a, "amounts") for a in amounts]
        if sum(fixed) != total:
            raise PlanError("INVALID_PLAN",
                            f"Сумма платежей ({sum(fixed)}) не равна сумме к оплате ({total}).")
        return frequency, [{"due_date": d, "amount": a, "locked": True}
                           for d, a in zip(days, fixed)]
    return frequency, [{"due_date": d, "amount": a, "locked": False}
                       for d, a in zip(days, split_evenly(total, len(days)))]


def normalize_plan(plan, *, price, start_date, end_date, phone,
                   total: Decimal | None = None) -> dict:
    """Validate a `payment_plan` body. Raises PlanError. No DB, no network.

    `total` is what the new installments must add up to — the whole price for
    a new contract, what is still unaccounted for when a plan is replaced.
    """
    if not isinstance(plan, dict):
        raise PlanError("INVALID_PLAN", "payment_plan должен быть объектом.")
    mode = plan.get("mode")
    if mode not in MODES:
        raise PlanError("INVALID_PLAN", f"payment_plan.mode должен быть одним из {MODES}.")
    price_dec = _whole_tenge(price, "price")
    total = price_dec if total is None else total

    raw_phone = plan.get("billing_phone") or phone
    if not raw_phone:
        raise PlanError("INVALID_PLAN",
                        "Нужен номер для счетов: payment_plan.billing_phone или phone договора.")
    try:
        billing_phone = apipay_client.normalize_phone(raw_phone)
    except ApiPayError as exc:
        raise PlanError("INVALID_PLAN", str(exc))

    channel = plan.get("channel")
    if channel is not None and channel not in CHANNELS:
        raise PlanError("INVALID_PLAN", f"payment_plan.channel должен быть одним из {CHANNELS}.")

    frequency, schedule = None, []
    if mode == MODE_STATIC and total > 0:
        frequency, schedule = _static_schedule(plan, total, start_date, end_date)
    return {"mode": mode, "billing_phone": billing_phone, "channel": channel,
            "frequency": frequency, "schedule": schedule, "price": price_dec}


def resolve_channel(billing_phone: str, requested: str | None = None) -> str:
    """Kaspi invoice when the number is in Kaspi, otherwise a WhatsApp link.

    Raises ApiPayError when the Kaspi check itself fails — the caller answers
    502 rather than guessing, exactly like bookings/batch does.
    """
    if requested == CHANNEL_LINK:
        return CHANNEL_LINK
    if not config.APIPAY_ENABLED:
        if requested == CHANNEL_KASPI:
            raise PlanError("INVALID_PLAN", "ApiPay не настроен — счёт в Kaspi выставить нельзя.")
        return CHANNEL_LINK
    from integrations import apipay_service  # local: apipay_service imports us lazily too
    try:
        apipay_service.ensure_kaspi_client(billing_phone)
    except apipay_client.KaspiClientMissing:
        if requested == CHANNEL_KASPI:
            raise PlanError("NO_KASPI", f"Номер {billing_phone} не зарегистрирован в Kaspi.")
        return CHANNEL_LINK
    return CHANNEL_KASPI


def channel_for_send(contract: dict) -> str:
    """The channel for an installment going out NOW.

    Decided at every send, not at creation: ApiPay may have been configured
    since, or the client may have joined Kaspi. Only a manager's explicit
    WhatsApp choice (`payment_channel_forced`) is kept. Calls ApiPay, so run
    it outside any transaction; raises ApiPayError when the check itself fails.
    """
    if contract.get("payment_channel_forced"):
        return CHANNEL_LINK
    return resolve_channel(contract["billing_phone"])


def _read_contract(contract_id: int) -> dict | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM contracts WHERE id = %s", (contract_id,))
            row = cur.fetchone()
    return dict(row) if row else None


def prepare_plan(plan, *, price, start_date, end_date, phone,
                 total: Decimal | None = None) -> tuple[dict | None, dict | None]:
    """normalize_plan + resolve_channel, as ``(prepared, error_envelope)``.

    Runs BEFORE any transaction: it may call ApiPay, and a slow network call
    must never hold row locks.
    """
    try:
        prepared = normalize_plan(plan, price=price, start_date=start_date,
                                  end_date=end_date, phone=phone, total=total)
        # Only an explicit WhatsApp choice outlives creation: every send
        # re-decides the channel (`channel_for_send`), so the answer here is
        # what the manager is shown now, not a promise for later installments.
        prepared["channel_forced"] = prepared["channel"] == CHANNEL_LINK
        prepared["channel"] = resolve_channel(prepared["billing_phone"], prepared["channel"])
    except PlanError as exc:
        return None, _err(exc.code, exc.message)
    except ApiPayError as exc:
        return None, _err("PAYMENT_PROVIDER_ERROR",
                          f"Не удалось проверить номер в Kaspi: {exc}")
    return prepared, None


# ---------------------------------------------------------------------------
# Writing plans (on the caller's cursor)
# ---------------------------------------------------------------------------

def _next_seq(cur, contract_id: int) -> int:
    cur.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM contract_installments "
                "WHERE contract_id = %s", (contract_id,))
    return cur.fetchone()["n"]


def _insert_booking_installments(cur, contract_id: int, booking_ids: list[int]) -> int:
    """One scheduled installment per booked SLOT of `booking_ids`.

    A day-crossing booking is two rows sharing `group_transition` but one slot:
    it gets one installment, keyed on its first row and due when the second
    ends. Slots that already have a live installment are skipped, so this is
    safe to call again for the same bookings.
    """
    if not booking_ids:
        return 0
    cur.execute(
        """SELECT g.booking_id, g.due_at
             FROM (SELECT MIN(b.id) AS booking_id,
                          COALESCE(MAX(b.end_at), NOW()) AS due_at,
                          MIN(b.start_at) AS start_at
                     FROM bookings b
                    WHERE b.id = ANY(%s) AND b.state <> ALL(%s)
                    GROUP BY COALESCE(b.group_transition::text, 'id:' || b.id)) g
            WHERE NOT EXISTS (SELECT 1 FROM contract_installments i
                               WHERE i.booking_id = g.booking_id AND i.status <> %s)
            ORDER BY g.start_at, g.booking_id""",
        (list(booking_ids), list(_DEAD_BOOKING_STATES), CANCELLED),
    )
    slots = cur.fetchall()
    seq = _next_seq(cur, contract_id)
    for k, slot in enumerate(slots):
        cur.execute(
            "INSERT INTO contract_installments (contract_id, seq, booking_id, due_at) "
            "VALUES (%s, %s, %s, %s)",
            (contract_id, seq + k, slot["booking_id"], slot["due_at"]),
        )
    return len(slots)


def _insert_static_installments(cur, contract_id: int, schedule: list[dict]) -> int:
    seq = _next_seq(cur, contract_id)
    for k, item in enumerate(schedule):
        cur.execute(
            "INSERT INTO contract_installments (contract_id, seq, due_at, amount, amount_locked) "
            "VALUES (%s, %s, %s, %s, %s)",
            (contract_id, seq + k, due_at_for(item["due_date"]), item["amount"], item["locked"]),
        )
    return len(schedule)


def write_plan(cur, contract_id: int, prepared: dict, booking_ids: list[int] | None = None) -> None:
    """Store a prepared plan and its installments on the caller's cursor.

    For a dynamic plan `booking_ids` are the contract's bookings to bill; the
    static schedule was already worked out by `normalize_plan`.
    """
    cur.execute(
        "UPDATE contracts SET payment_mode = %s, payment_frequency = %s, "
        "  billing_phone = %s, payment_channel = %s, payment_channel_forced = %s, "
        "  updated_at = NOW() "
        "WHERE id = %s",
        (prepared["mode"], prepared["frequency"], prepared["billing_phone"],
         prepared["channel"], prepared.get("channel_forced", False), contract_id),
    )
    if prepared["mode"] == MODE_STATIC:
        _insert_static_installments(cur, contract_id, prepared["schedule"])
    else:
        _insert_booking_installments(cur, contract_id, booking_ids or [])
    refresh_payment_status(cur, contract_id)


def add_booking_installments(cur, contract_id: int, booking_ids: list[int]) -> int:
    """New bookings on a dynamic contract each get an installment. No-op otherwise."""
    cur.execute("SELECT payment_mode FROM contracts WHERE id = %s", (contract_id,))
    row = cur.fetchone()
    if not row or row["payment_mode"] != MODE_DYNAMIC:
        return 0
    added = _insert_booking_installments(cur, contract_id, booking_ids)
    if added:
        refresh_payment_status(cur, contract_id)
    return added


def _money_state(cur, contract_id: int, exclude_id: int | None = None) -> dict:
    """What is paid, owed, fixed and still open on a contract's installments."""
    cur.execute(
        """SELECT
             COALESCE(SUM(paid_amount) FILTER (WHERE status = %s), 0) AS paid,
             COALESCE(SUM(amount) FILTER (WHERE status = ANY(%s)), 0) AS owed,
             COALESCE(SUM(amount) FILTER (WHERE status = %s AND amount IS NOT NULL
                                           AND id IS DISTINCT FROM %s), 0) AS fixed,
             COUNT(*) FILTER (WHERE status = %s AND amount IS NULL) AS open_count
           FROM contract_installments WHERE contract_id = %s""",
        (PAID, list(_OWED), SCHEDULED, exclude_id, SCHEDULED, contract_id),
    )
    row = cur.fetchone()
    return {k: (Decimal(row[k]) if k != "open_count" else int(row[k])) for k in row}


def _amount_for(cur, contract: dict, inst: dict) -> Decimal:
    """What this installment asks for when it is issued.

    A fixed amount (static plan, or locked by a manager) is used as is.
    Otherwise it is an equal share of what is left after everything paid,
    already invoiced, or fixed on other installments — the dynamic formula.
    """
    if inst.get("amount") is not None:
        return Decimal(inst["amount"])
    state = _money_state(cur, contract["id"], exclude_id=inst["id"])
    remaining = Decimal(contract["price"]) - state["paid"] - state["owed"] - state["fixed"]
    if remaining <= 0:
        return Decimal(0)
    parts = max(state["open_count"], 1)
    return remaining if parts == 1 else (remaining / parts).to_integral_value(rounding="ROUND_FLOOR")


def rebalance(cur, contract_id: int, strict: bool = True) -> None:
    """Re-spread a static contract's price over its not-yet-issued, unlocked installments.

    Called after anything that changes what is left to schedule: a new price,
    a manager fixing one installment's amount, or a manual payment of a
    different amount than was asked.

    `strict` (a manager's own edit): raises PlanError when the fixed parts
    exceed the price — the caller's transaction rolls back. Not strict (money
    that already arrived): nothing is refused; installments with nothing left
    to cover are cancelled instead of being billed.
    """
    cur.execute("SELECT id, price, payment_mode FROM contracts WHERE id = %s", (contract_id,))
    contract = cur.fetchone()
    if not contract or contract["payment_mode"] != MODE_STATIC:
        return
    cur.execute(
        "SELECT id, amount, amount_locked FROM contract_installments "
        "WHERE contract_id = %s AND status = %s ORDER BY seq",
        (contract_id, SCHEDULED),
    )
    scheduled = cur.fetchall()
    free = [r for r in scheduled if not r["amount_locked"]]
    locked = sum((Decimal(r["amount"]) for r in scheduled if r["amount_locked"]), Decimal(0))
    state = _money_state(cur, contract_id)
    remaining = Decimal(contract["price"]) - state["paid"] - state["owed"] - locked
    if strict and remaining < 0:
        raise PlanError("INVALID_AMOUNT",
                        f"Сумма платежей превышает цену договора на {-remaining}₸.")
    if not free:
        if remaining:
            logger.warning("[CONTRACT] Договор %s: %s₸ не распределены — все платежи "
                           "зафиксированы вручную", contract_id, remaining)
        return
    if strict and remaining < len(free):
        raise PlanError("INVALID_AMOUNT",
                        "Остатка не хватает на оставшиеся платежи — уменьшите сумму.")
    amounts = (split_evenly(remaining, len(free)) if remaining > 0
               else [Decimal(0)] * len(free))
    for row, amount in zip(free, amounts):
        if amount > 0:
            cur.execute("UPDATE contract_installments SET amount = %s, updated_at = NOW() "
                        "WHERE id = %s", (amount, row["id"]))
        else:
            cur.execute("UPDATE contract_installments SET status = %s, "
                        "  last_error = 'nothing_left_to_pay', updated_at = NOW() "
                        "WHERE id = %s", (CANCELLED, row["id"]))


def refresh_payment_status(cur, contract_id: int) -> str:
    """Roll the installments up into contracts.payment_status.

    none (no plan) · scheduled · awaiting (an invoice is out) · overdue (retries
    used up, needs a manager) · paid (price collected) · stopped (plan halted
    before the price was collected) · cancelled (contract cancelled).
    """
    cur.execute("SELECT status, payment_mode, price FROM contracts WHERE id = %s", (contract_id,))
    contract = cur.fetchone()
    if not contract:
        return "none"
    cur.execute(
        "SELECT status, COUNT(*) AS n, COALESCE(SUM(paid_amount), 0) AS paid "
        "FROM contract_installments WHERE contract_id = %s GROUP BY status",
        (contract_id,),
    )
    by_status = {r["status"]: r for r in cur.fetchall()}
    paid_total = Decimal(by_status[PAID]["paid"]) if PAID in by_status else Decimal(0)

    if contract["payment_mode"] is None:
        status = "none"
    elif contract["status"] in ("cancelled", "failed"):
        status = "cancelled"
    elif OVERDUE in by_status:
        status = OVERDUE
    elif ISSUED in by_status:
        status = "awaiting"
    elif SCHEDULED in by_status:
        status = SCHEDULED
    elif paid_total >= Decimal(contract["price"]) and PAID in by_status:
        status = PAID
    else:
        status = "stopped" if by_status else SCHEDULED
    cur.execute("UPDATE contracts SET payment_status = %s WHERE id = %s", (status, contract_id))
    return status


# ---------------------------------------------------------------------------
# Issuing — the scheduler's job
# ---------------------------------------------------------------------------

def _description(cur, contract_id: int, inst: dict) -> str:
    """What the payer sees in Kaspi — only the first 60 characters are shown.

    Numbered among the LIVE installments, not by `seq`: a replaced plan keeps
    its cancelled rows, and "платёж 5 из 3" would read as a mistake.
    """
    if inst.get("booking_date"):
        return f"Договор №{contract_id}: бронь {inst['booking_date']:%d.%m.%Y}"
    cur.execute(
        "SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE seq <= %s) AS k "
        "FROM contract_installments WHERE contract_id = %s AND status <> %s",
        (inst["seq"], contract_id, CANCELLED),
    )
    pos = cur.fetchone()
    return f"Договор №{contract_id}: платёж {pos['k']} из {pos['n']}"


def _load_for_issue(cur, installment_id: int, force: bool) -> dict | None:
    cur.execute(
        "SELECT i.*, b.state AS booking_state, b.date AS booking_date "
        "  FROM contract_installments i "
        "  LEFT JOIN bookings b ON b.id = i.booking_id "
        " WHERE i.id = %s AND i.status = %s AND (%s OR i.due_at <= NOW()) "
        "   FOR UPDATE OF i SKIP LOCKED",
        (installment_id, SCHEDULED, force),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _cancel_installment(cur, inst: dict, reason: str) -> None:
    cur.execute("UPDATE contract_installments SET status = %s, last_error = %s, "
                "  updated_at = NOW() WHERE id = %s", (CANCELLED, reason, inst["id"]))
    refresh_payment_status(cur, inst["contract_id"])
    logger.info("[CONTRACT] Платёж %s договора %s снят: %s",
                inst["id"], inst["contract_id"], reason)


def issue_installment(installment_id: int, force: bool = False,
                      channel: str | None = None) -> dict | None:
    """Ask for one due installment. Returns what was sent, or None if nothing was.

    The channel is decided first, outside any transaction (`channel_for_send`
    may call ApiPay's Kaspi check; pass `channel` to reuse one already decided).
    Raises ApiPayError when that check fails — the installment stays scheduled.

    The amount is fixed and the invoice row queued in one transaction, with the
    contract row locked so two workers cannot both work out a dynamic share
    from the same "what is left". ApiPay / WhatsApp are reached only after the
    commit; a failed Kaspi send stays in the ApiPay outbox and is retried by
    `apipay_service.sweep_pending_sends`.
    """
    if channel is None:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT c.* FROM contract_installments i "
                            "JOIN contracts c ON c.id = i.contract_id WHERE i.id = %s",
                            (installment_id,))
                row = cur.fetchone()
        if row is None:
            return None
        channel = channel_for_send(dict(row))

    queued = None
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            inst = _load_for_issue(cur, installment_id, force)
            if inst is None:
                return None
            cur.execute("SELECT * FROM contracts WHERE id = %s FOR UPDATE", (inst["contract_id"],))
            contract = dict(cur.fetchone())

            if contract["status"] in ("cancelled", "failed"):
                _cancel_installment(cur, inst, "contract_cancelled")
                return None
            if contract["payment_mode"] == MODE_DYNAMIC and (
                    inst["booking_id"] is None or inst["booking_state"] in _DEAD_BOOKING_STATES):
                _cancel_installment(cur, inst, "booking_cancelled")
                return None

            amount = _amount_for(cur, contract, inst)
            if amount <= 0:
                _cancel_installment(cur, inst, "nothing_left_to_pay")
                return None

            description = _description(cur, contract["id"], inst)

            if channel == CHANNEL_KASPI and not config.APIPAY_ENABLED:
                channel = CHANNEL_LINK
            # The contract shows the channel its latest installment went out on.
            cur.execute("UPDATE contracts SET payment_channel = %s WHERE id = %s",
                        (channel, contract["id"]))
            row_id = None
            if channel == CHANNEL_KASPI:
                external_order_id = (f"contract-{contract['id']}-{inst['id']}-"
                                     f"{uuid.uuid4().hex[:8]}")
                row_id = apipay_repo.insert_invoice(
                    cur, external_order_id, contract["billing_phone"], amount, [],
                    source="contract", contract_installment_id=inst["id"])
                queued = {"id": None, "row_id": row_id, "amount": int(amount),
                          "phone": contract["billing_phone"],
                          "external_order_id": external_order_id,
                          "description": description, "booking_ids": []}

            cur.execute(
                "UPDATE contract_installments SET status = %s, amount = %s, channel = %s, "
                "  attempts = attempts + 1, apipay_row_id = %s, issued_at = NOW(), "
                "  last_error = NULL, updated_at = NOW() WHERE id = %s",
                (ISSUED, amount, channel, row_id, inst["id"]),
            )
            refresh_payment_status(cur, contract["id"])

    sent = {"installment_id": installment_id, "contract_id": contract["id"],
            "amount": int(amount), "channel": channel, "description": description}
    if queued is not None:
        from integrations import apipay_service
        try:
            invoice = apipay_service.send_invoice(queued)
            sent["invoice_id"] = invoice["invoice_id"]
        except ApiPayError:
            # Committed and queued: the outbox sweep retries it.
            sent["invoice_id"] = None
            sent["send_pending"] = True
    else:
        sent["notified"] = _send_link(contract, amount, description)
    logger.info("[CONTRACT] Договор %s: платёж %s на %s₸ выставлен (%s)",
                contract["id"], installment_id, amount, channel)
    return sent


def _send_link(contract: dict, amount, description: str) -> bool:
    """The not-in-Kaspi path: amount and payment link on WhatsApp.

    Settled by a manager (`mark_installment_paid`) once the receipt arrives —
    nothing automated can see a payment made this way.
    """
    return client_notify.send(
        contract["billing_phone"], "contract_payment_link",
        contract_id=contract["id"], description=description,
        amount=client_notify.fmt_amount(amount), link=config.KASPI_PAYMENT_URL)


def issue_due_installments(limit: int = 20) -> int:
    """Issue every installment whose due time has come. Returns how many went out.

    Runs in every gunicorn worker; `issue_installment` takes each row with
    SKIP LOCKED and re-checks its status, so two sweeps never issue one twice.
    """
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id FROM contract_installments "
                    " WHERE status = %s AND due_at <= NOW() "
                    " ORDER BY due_at, id LIMIT %s",
                    (SCHEDULED, limit),
                )
                ids = [r["id"] for r in cur.fetchall()]
    except Exception:
        logger.exception("[CONTRACT] Не удалось выбрать платежи к выставлению")
        return 0

    issued = 0
    for installment_id in ids:
        try:
            if issue_installment(installment_id):
                issued += 1
        except ApiPayError as exc:
            # The Kaspi check failed: nothing was written, the installment is
            # still scheduled, and the next sweep (a minute later) tries again.
            logger.warning("[CONTRACT] Платёж %s не выставлен — проверка Kaspi не удалась: %s",
                           installment_id, exc)
        except Exception:
            logger.exception("[CONTRACT] Ошибка выставления платежа %s", installment_id)
    return issued


def resend_queued(row: dict) -> bool:
    """Outbox retry for an installment invoice (called by sweep_pending_sends).

    Sent only if it is still the invoice the installment is waiting on — a
    manager may have marked it paid or cancelled the contract in the meantime.
    """
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT i.*, b.date AS booking_date FROM contract_installments i "
                "  LEFT JOIN bookings b ON b.id = i.booking_id WHERE i.id = %s",
                (row["contract_installment_id"],),
            )
            inst = cur.fetchone()
            description = _description(cur, inst["contract_id"], inst) if inst else ""
    if not inst or inst["status"] != ISSUED or inst["apipay_row_id"] != row["id"]:
        apipay_repo.cancel_unsent_row(row["id"], "installment_not_open")
        return False
    from integrations import apipay_service
    apipay_service.send_invoice({
        "row_id": row["id"], "amount": int(row["amount"]), "phone": row["phone"],
        "external_order_id": row["external_order_id"], "booking_ids": [],
        "description": description,
    })
    return True


# ---------------------------------------------------------------------------
# Webhook outcomes (on the webhook's cursor — see apipay_service.apply_webhook_status)
# ---------------------------------------------------------------------------

def _flag_for_refund(cur, invoice_row: dict, why: str) -> None:
    logger.error("[CONTRACT] ⚠️ ТРЕБУЕТСЯ РУЧНОЙ ВОЗВРАТ: счёт %s (%s₸) оплачен, но %s. "
                 "Верните деньги через личный кабинет ApiPay.",
                 invoice_row.get("invoice_id"), invoice_row.get("amount"), why)
    cur.execute("UPDATE apipay_invoices SET error_code = 'paid_after_cancellation', "
                "  error_message = %s, updated_at = NOW() WHERE id = %s",
                (f"Платёж по договору: {why} — нужен возврат {invoice_row.get('amount')}₸",
                 invoice_row["id"]))


def apply_invoice_status(cur, invoice_row: dict, status: str) -> dict:
    """Apply an installment invoice's new status. Same transaction as the claim.

    Returns the usual `apply_webhook_status` shape, with `contract` naming what
    happened (`paid`, `retry_scheduled`, `overdue`, …) for `after_transition`.
    """
    result = {"invoice": invoice_row, "changed": [], "released": False, "paid": False,
              "status": status, "contract": {"event": None}}
    cur.execute("SELECT * FROM contract_installments WHERE id = %s FOR UPDATE",
                (invoice_row["contract_installment_id"],))
    inst = cur.fetchone()
    if inst is None:
        logger.warning("[CONTRACT] Счёт %s ссылается на удалённый платёж %s",
                       invoice_row.get("invoice_id"), invoice_row["contract_installment_id"])
        return result
    result["contract"].update(installment_id=inst["id"], contract_id=inst["contract_id"])

    if status == apipay_client.STATUS_PAID:
        if inst["status"] == PAID:
            _flag_for_refund(cur, invoice_row, f"платёж {inst['id']} уже был оплачен")
            result["contract"]["event"] = "double_payment"
        elif inst["status"] == CANCELLED:
            _flag_for_refund(cur, invoice_row, f"платёж {inst['id']} был отменён")
            result["contract"]["event"] = "paid_after_cancellation"
        else:
            cur.execute(
                "UPDATE contract_installments SET status = %s, paid_at = NOW(), "
                "  paid_amount = %s, paid_via = 'apipay', last_error = NULL, "
                "  updated_at = NOW() WHERE id = %s",
                (PAID, invoice_row["amount"], inst["id"]),
            )
            result["paid"] = True
            result["contract"]["event"] = "paid"
            logger.info("[CONTRACT] Договор %s: платёж %s оплачен (%s₸, счёт %s)",
                        inst["contract_id"], inst["id"], invoice_row["amount"],
                        invoice_row.get("invoice_id"))
    elif status in apipay_client.FAILED_STATUSES:
        # Only the invoice the installment is currently waiting on counts: a
        # late 'expired' for an older attempt must not reschedule a newer one.
        if inst["status"] == ISSUED and inst["apipay_row_id"] == invoice_row["id"]:
            if inst["attempts"] < config.CONTRACT_INVOICE_MAX_ATTEMPTS:
                cur.execute(
                    "UPDATE contract_installments SET status = %s, last_error = %s, "
                    "  due_at = NOW() + make_interval(hours => %s), updated_at = NOW() "
                    "WHERE id = %s",
                    (SCHEDULED, f"invoice {status}", config.CONTRACT_INVOICE_RETRY_HOURS,
                     inst["id"]),
                )
                result["contract"]["event"] = "retry_scheduled"
            else:
                cur.execute(
                    "UPDATE contract_installments SET status = %s, last_error = %s, "
                    "  updated_at = NOW() WHERE id = %s",
                    (OVERDUE, f"invoice {status}; попыток: {inst['attempts']}", inst["id"]),
                )
                result["contract"]["event"] = "overdue"
                logger.warning("[CONTRACT] Договор %s: платёж %s просрочен после %s счетов",
                               inst["contract_id"], inst["id"], inst["attempts"])
    elif status == apipay_client.STATUS_REFUNDED:
        cur.execute("UPDATE contract_installments SET last_error = 'refunded', "
                    "  updated_at = NOW() WHERE id = %s", (inst["id"],))
        result["contract"]["event"] = "refunded"
    refresh_payment_status(cur, inst["contract_id"])
    return result


def after_invoice_transition(invoice_row: dict, paid: bool) -> None:
    """Post-commit side of a paid installment: tell the client, close duplicates.

    Never raises — the installment is already recorded.
    """
    if not paid:
        return
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT i.id, i.contract_id, c.billing_phone FROM contract_installments i "
                    "JOIN contracts c ON c.id = i.contract_id WHERE i.id = %s",
                    (invoice_row["contract_installment_id"],),
                )
                inst = cur.fetchone()
                others = _open_invoices(cur, [invoice_row["contract_installment_id"]],
                                        exclude_row=invoice_row["id"])
    except Exception:
        logger.exception("[CONTRACT] Не удалось прочитать платёж после оплаты")
        return
    _retire_invoices(others, "installment_paid")
    if inst:
        client_notify.send(inst["billing_phone"], "contract_installment_paid",
                           contract_id=inst["contract_id"],
                           amount=client_notify.fmt_amount(invoice_row.get("amount")))


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

def _open_invoices(cur, installment_ids: list[int], exclude_row: int | None = None) -> list[dict]:
    if not installment_ids:
        return []
    cur.execute(
        "SELECT * FROM apipay_invoices WHERE contract_installment_id = ANY(%s) "
        "   AND status = ANY(%s) AND id IS DISTINCT FROM %s",
        (list(installment_ids), list(apipay_client.OPEN_STATUSES), exclude_row),
    )
    return [dict(r) for r in cur.fetchall()]


def _retire_invoices(rows: list[dict], reason: str) -> None:
    """Take back invoices nobody should pay any more. After the commit; never raises.

    An unsent row just leaves the outbox. A sent one is cancelled at ApiPay; if
    ApiPay refuses (typically: already paid) the row stays open and the webhook
    decides — a payment then lands on a cancelled installment and is flagged
    for a manual refund.
    """
    for row in rows:
        try:
            if row.get("invoice_id") is None:
                apipay_repo.cancel_unsent_row(row["id"], reason)
                continue
            if test_context.is_test_mode():
                continue
            apipay_client.cancel_invoice(row["invoice_id"])
            apipay_repo.mark_cancelled(row["invoice_id"], reason)
            logger.info("[CONTRACT] Счёт %s отменён (%s)", row["invoice_id"], reason)
        except ApiPayError as exc:
            logger.warning("[CONTRACT] Не удалось отменить счёт %s: %s", row.get("invoice_id"), exc)
        except Exception:
            logger.exception("[CONTRACT] Ошибка отмены счёта %s", row.get("invoice_id"))


def _cancel_where(cur, where_sql: str, params: list, reason: str) -> list[dict]:
    """Cancel the matching unpaid installments; returns their open invoices."""
    cur.execute(
        "UPDATE contract_installments SET status = %s, last_error = %s, updated_at = NOW() "
        f"WHERE {where_sql} AND status = ANY(%s) RETURNING id",
        [CANCELLED, reason, *params, [SCHEDULED, ISSUED, OVERDUE]],
    )
    ids = [r["id"] for r in cur.fetchall()]
    return _open_invoices(cur, ids)


def on_contract_cancelled(contract_id: int, reason: str = "contract_cancel") -> None:
    """The contract is gone: stop every unpaid installment. Never raises."""
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                rows = _cancel_where(cur, "contract_id = %s", [contract_id], reason)
                refresh_payment_status(cur, contract_id)
    except Exception:
        logger.exception("[CONTRACT] Не удалось снять платежи договора %s", contract_id)
        return
    _retire_invoices(rows, reason)


def on_contract_bookings_cancelled(contract_id: int, booking_ids: list[int],
                                   reason: str = "contract_booking_cancel") -> None:
    """Dynamic plans bill per booking: a cancelled booking takes its installment
    with it, and the next issued share grows to cover the price. Never raises."""
    if not booking_ids:
        return
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT payment_mode FROM contracts WHERE id = %s", (contract_id,))
                row = cur.fetchone()
                if not row or row["payment_mode"] != MODE_DYNAMIC:
                    return
                rows = _cancel_where(cur, "contract_id = %s AND booking_id = ANY(%s)",
                                     [contract_id, list(booking_ids)], reason)
                refresh_payment_status(cur, contract_id)
    except Exception:
        logger.exception("[CONTRACT] Не удалось снять платежи броней %s", booking_ids)
        return
    _retire_invoices(rows, reason)


def on_price_changed(cur, contract_id: int) -> None:
    """A new contract price re-spreads the static schedule (dynamic shares
    already follow the price at issue time). Raises PlanError on overflow."""
    rebalance(cur, contract_id)
    refresh_payment_status(cur, contract_id)


# ---------------------------------------------------------------------------
# Manager operations
# ---------------------------------------------------------------------------

def _installment_dict(row: dict) -> dict:
    out = dict(row)
    for key in ("amount", "paid_amount"):
        if out.get(key) is not None:
            out[key] = float(out[key])
    return out


def get_plan(contract_id: int) -> dict:
    """The contract's plan, installments, and money summary for the manager UI."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, price, status, payment_mode, payment_frequency, billing_phone, "
                "  payment_channel, payment_channel_forced, payment_status "
                "FROM contracts WHERE id = %s",
                (contract_id,),
            )
            contract = cur.fetchone()
            if not contract:
                return _err("NOT_FOUND", "Договор не найден.")
            cur.execute(
                "SELECT i.*, a.invoice_id, a.status AS invoice_status "
                "  FROM contract_installments i "
                "  LEFT JOIN apipay_invoices a ON a.id = i.apipay_row_id "
                " WHERE i.contract_id = %s ORDER BY i.seq",
                (contract_id,),
            )
            installments = [_installment_dict(r) for r in cur.fetchall()]
            state = _money_state(cur, contract_id)
    price = Decimal(contract["price"])
    open_share = None
    if contract["payment_mode"] == MODE_DYNAMIC and state["open_count"]:
        left = price - state["paid"] - state["owed"] - state["fixed"]
        open_share = float(max(left, Decimal(0)) / state["open_count"])
    upcoming = [i for i in installments if i["status"] == SCHEDULED]
    return _ok({
        "contract_id": contract_id,
        "payment_mode": contract["payment_mode"],
        "payment_frequency": contract["payment_frequency"],
        "billing_phone": contract["billing_phone"],
        "payment_channel": contract["payment_channel"],
        # True: a manager chose the WhatsApp link — Kaspi is never used.
        "payment_channel_forced": contract["payment_channel_forced"],
        "payment_status": contract["payment_status"],
        "summary": {
            "price": float(price),
            "paid": float(state["paid"]),
            "invoiced_unpaid": float(state["owed"]),
            "left_to_pay": float(price - state["paid"]),
            "next_due_at": upcoming[0]["due_at"] if upcoming else None,
            # Dynamic mode: what each not-yet-issued booking would be billed now.
            "per_booking_estimate": open_share,
        },
        "installments": installments,
    })


def replace_plan(contract_id: int, plan: dict) -> dict:
    """Set or replace a contract's plan.

    Issued, overdue and paid installments stay as they are — money already
    asked for is not re-asked. Scheduled ones are cancelled and the new plan
    covers what remains: price - paid - still invoiced.
    """
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM contracts WHERE id = %s", (contract_id,))
            contract = cur.fetchone()
            if contract:
                state = _money_state(cur, contract_id)
    if not contract:
        return _err("NOT_FOUND", "Договор не найден.")
    if contract["status"] in ("cancelled", "failed"):
        return _err("INVALID_STATE", "Договор отменён — план оплаты не меняется.")

    total = max(Decimal(contract["price"]) - state["paid"] - state["owed"], Decimal(0))
    prepared, error = prepare_plan(plan, price=contract["price"],
                                   start_date=contract["start_date"],
                                   end_date=contract["end_date"],
                                   phone=contract["phone"], total=total)
    if error:
        return error
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT id FROM contracts WHERE id = %s FOR UPDATE", (contract_id,))
                cur.execute("UPDATE contract_installments SET status = %s, "
                            "  last_error = 'plan_replaced', updated_at = NOW() "
                            "WHERE contract_id = %s AND status = %s",
                            (CANCELLED, contract_id, SCHEDULED))
                cur.execute("SELECT booking_id FROM contract_bookings WHERE contract_id = %s",
                            (contract_id,))
                booking_ids = [r["booking_id"] for r in cur.fetchall()]
                write_plan(cur, contract_id, prepared, booking_ids)
    except PlanError as exc:
        return _err(exc.code, exc.message)
    return get_plan(contract_id)


def stop_plan(contract_id: int) -> dict:
    """Stop billing: every unpaid installment is cancelled, paid ones are kept."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id FROM contracts WHERE id = %s FOR UPDATE", (contract_id,))
            if not cur.fetchone():
                return _err("NOT_FOUND", "Договор не найден.")
            rows = _cancel_where(cur, "contract_id = %s", [contract_id], "plan_stopped")
            refresh_payment_status(cur, contract_id)
    _retire_invoices(rows, "plan_stopped")
    return get_plan(contract_id)


def _lock_installment(cur, contract_id: int, installment_id: int) -> tuple[dict, dict]:
    cur.execute("SELECT * FROM contracts WHERE id = %s FOR UPDATE", (contract_id,))
    contract = cur.fetchone()
    cur.execute("SELECT i.*, b.date AS booking_date FROM contract_installments i "
                "  LEFT JOIN bookings b ON b.id = i.booking_id "
                "WHERE i.id = %s AND i.contract_id = %s FOR UPDATE OF i",
                (installment_id, contract_id))
    inst = cur.fetchone()
    if not contract or not inst:
        raise PlanError("NOT_FOUND", "Платёж не найден.")
    return dict(contract), dict(inst)


def update_installment(contract_id: int, installment_id: int, fields: dict) -> dict:
    """Adjust a not-yet-issued installment: `amount` (null = back to automatic)
    and/or `due_date`. A static plan re-spreads the rest to keep the price."""
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                contract, inst = _lock_installment(cur, contract_id, installment_id)
                if inst["status"] != SCHEDULED:
                    raise PlanError("INVALID_STATE",
                                    "Менять можно только ещё не выставленный платёж.")
                if "amount" in fields:
                    if fields["amount"] is None:
                        amount = (inst["amount"] if contract["payment_mode"] == MODE_STATIC
                                  else None)
                        cur.execute("UPDATE contract_installments SET amount = %s, "
                                    "  amount_locked = FALSE, updated_at = NOW() WHERE id = %s",
                                    (amount, installment_id))
                    else:
                        amount = _whole_tenge(fields["amount"], "amount")
                        cur.execute("UPDATE contract_installments SET amount = %s, "
                                    "  amount_locked = TRUE, updated_at = NOW() WHERE id = %s",
                                    (amount, installment_id))
                    rebalance(cur, contract_id)
                if fields.get("due_date"):
                    day = _parse_date(fields["due_date"], "due_date")
                    cur.execute("UPDATE contract_installments SET due_at = %s, "
                                "  updated_at = NOW() WHERE id = %s",
                                (due_at_for(day), installment_id))
                refresh_payment_status(cur, contract_id)
    except PlanError as exc:
        return _err(exc.code, exc.message)
    return get_plan(contract_id)


def mark_installment_paid(contract_id: int, installment_id: int, amount=None,
                          note: str | None = None) -> dict:
    """A manager confirms money that arrived another way (link, cash, transfer).

    Any invoice still open for it is taken back, so the client is not asked twice.
    """
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                contract, inst = _lock_installment(cur, contract_id, installment_id)
                if inst["status"] in (PAID, CANCELLED):
                    raise PlanError("INVALID_STATE", "Платёж уже оплачен или отменён.")
                paid = (_whole_tenge(amount, "amount") if amount is not None
                        else _amount_for(cur, contract, inst))
                if paid <= 0:
                    raise PlanError("INVALID_AMOUNT", "Нечего оплачивать: остаток по договору 0.")
                cur.execute(
                    "UPDATE contract_installments SET status = %s, paid_amount = %s, "
                    "  amount = COALESCE(amount, %s), paid_via = 'manual', paid_at = NOW(), "
                    "  last_error = %s, updated_at = NOW() WHERE id = %s",
                    (PAID, paid, paid, note, installment_id),
                )
                # Paid more (or less) than asked: the rest of a static schedule
                # follows, so the client is never billed past the price.
                rebalance(cur, contract_id, strict=False)
                rows = _open_invoices(cur, [installment_id])
                refresh_payment_status(cur, contract_id)
    except PlanError as exc:
        return _err(exc.code, exc.message)
    _retire_invoices(rows, "paid_manually")
    return get_plan(contract_id)


def send_installment_now(contract_id: int, installment_id: int) -> dict:
    """Issue a scheduled or overdue installment right away, or resend a link.

    An installment already sent as a WhatsApp link is upgraded to a Kaspi
    invoice when Kaspi is available now (ApiPay configured since, or the client
    joined Kaspi) — the link can't be tracked, the invoice can.
    """
    contract = _read_contract(contract_id)
    if contract is None:
        return _err("NOT_FOUND", "Договор не найден.")
    try:
        channel = channel_for_send(contract)
    except PlanError as exc:
        return _err(exc.code, exc.message)
    except ApiPayError as exc:
        return _err("PAYMENT_PROVIDER_ERROR", f"Не удалось проверить номер в Kaspi: {exc}")

    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                contract, inst = _lock_installment(cur, contract_id, installment_id)
                if inst["status"] == ISSUED:
                    if inst["channel"] != CHANNEL_LINK:
                        raise PlanError("INVALID_STATE",
                                        "Счёт уже выставлен в Kaspi — дождитесь оплаты или истечения.")
                    if channel == CHANNEL_LINK:
                        resend = (contract, inst, _description(cur, contract_id, inst))
                    else:
                        # Same amount, now as a Kaspi invoice.
                        resend = None
                        cur.execute(
                            "UPDATE contract_installments SET status = %s, due_at = NOW(), "
                            "  updated_at = NOW() WHERE id = %s",
                            (SCHEDULED, installment_id),
                        )
                elif inst["status"] in (SCHEDULED, OVERDUE):
                    resend = None
                    # An overdue installment gets exactly one more invoice.
                    cur.execute(
                        "UPDATE contract_installments SET status = %s, due_at = NOW(), "
                        "  attempts = LEAST(attempts, %s), updated_at = NOW() WHERE id = %s",
                        (SCHEDULED, max(config.CONTRACT_INVOICE_MAX_ATTEMPTS - 1, 0),
                         installment_id),
                    )
                else:
                    raise PlanError("INVALID_STATE", "Платёж уже оплачен или отменён.")
    except PlanError as exc:
        return _err(exc.code, exc.message)

    if resend:
        contract, inst, description = resend
        notified = _send_link(contract, inst["amount"], description)
        return _ok({"installment_id": installment_id, "channel": CHANNEL_LINK,
                    "notified": notified})

    sent = issue_installment(installment_id, force=True, channel=channel)
    if sent is None:
        return _err("INVALID_STATE", "Платёж не выставлен: договор отменён или остаток 0.")
    return _ok(sent)


def preview_plan(plan, *, price, start_date, end_date, phone, slots=None) -> dict:
    """What a plan WOULD produce — for the creation modal. No DB writes, no Kaspi check."""
    from integrations import booking_service

    try:
        prepared = normalize_plan(plan, price=price, start_date=start_date,
                                  end_date=end_date, phone=phone)
    except PlanError as exc:
        return _err(exc.code, exc.message)
    if prepared["mode"] == MODE_STATIC:
        installments = [{"seq": k + 1, "due_at": due_at_for(i["due_date"]).isoformat(),
                         "amount": float(i["amount"])}
                        for k, i in enumerate(prepared["schedule"])]
    else:
        count = booking_service.count_slot_occurrences(slots or [])
        installments = []
        if count:
            installments = [{"seq": k + 1, "amount": float(a)}
                            for k, a in enumerate(split_evenly(prepared["price"], count))]
    return _ok({"mode": prepared["mode"], "billing_phone": prepared["billing_phone"],
                "frequency": prepared["frequency"], "installments": installments,
                "installments_count": len(installments)})
