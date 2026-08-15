"""ApiPay.kz HTTP client — online avans collection via Kaspi Pay.

Docs: https://apipay.kz/for-ai

Only this module talks to ApiPay. It raises ``ApiPayError`` on any failure
(network, timeout, non-2xx, malformed body) so callers can treat "the invoice
was not sent" as a single condition — the outbox in `apipay_service` relies on
that to decide what to retry.

The API key is server-side only: never expose it to the browser or the Apps
Script manager UI.
"""

import hashlib
import hmac
import logging
import re

import requests

import config

logger = logging.getLogger(__name__)

# Statuses ApiPay reports. processing/pending are in-flight; the rest terminal.
STATUS_PAID = "paid"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"
STATUS_ERROR = "error"
STATUS_REFUNDED = "refunded"

# Ours, not ApiPay's: the row is committed but `POST /invoices` has not
# succeeded yet, so the client has been asked for nothing. Distinguishable from
# a sent invoice by `invoice_id IS NULL`. See migration 039.
STATUS_CREATED = "created"

OPEN_STATUSES = (STATUS_CREATED, "processing", "pending")
FAILED_STATUSES = (STATUS_CANCELLED, STATUS_EXPIRED, STATUS_ERROR)


class ApiPayError(RuntimeError):
    """Any ApiPay call that did not return a usable result."""

    def __init__(self, message: str, status_code: int | None = None,
                 payload: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


class KaspiClientMissing(ApiPayError):
    """The phone is not registered in Kaspi — an invoice to it can never be paid.

    A subclass, so every existing ``except ApiPayError`` keeps covering it, but
    distinguishable where the answer has to say "this client has no Kaspi"
    rather than "the payment provider failed" — the two need different wording
    and different HTTP codes.
    """

    def __init__(self, phone: str):
        super().__init__(f"Номер {phone} не зарегистрирован в Kaspi — "
                         "счёт на аванс выставить некуда.")
        self.phone = phone


def normalize_phone(phone: str | None) -> str:
    """Convert a KZ phone to ApiPay's strict ``8XXXXXXXXXX`` form.

    Accepts +7…, 7…, 8…, and any spacing/bracket noise. Raises ApiPayError if
    the result is not 11 digits starting with 8 — an invalid phone would come
    back as a 422 from ApiPay anyway, and failing here keeps the error local.
    """
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10 and digits.startswith("7"):
        digits = "8" + digits          # 7001234567 → 87001234567
    elif len(digits) == 11 and digits.startswith("7"):
        digits = "8" + digits[1:]      # 77001234567 → 87001234567
    if len(digits) != 11 or not digits.startswith("8"):
        raise ApiPayError(f"Некорректный номер телефона для ApiPay: {phone!r}")
    return digits


def _request(method: str, path: str, json_body: dict | None = None) -> dict:
    if not config.APIPAY_API_KEY:
        raise ApiPayError("APIPAY_API_KEY не задан.")
    url = f"{config.APIPAY_BASE_URL.rstrip('/')}{path}"
    try:
        resp = requests.request(
            method, url, json=json_body, timeout=config.APIPAY_TIMEOUT,
            headers={"X-API-Key": config.APIPAY_API_KEY,
                     "Content-Type": "application/json"},
        )
    except requests.RequestException as exc:
        raise ApiPayError(f"ApiPay недоступен: {exc}") from exc

    try:
        payload = resp.json()
    except ValueError:
        payload = {}

    if resp.status_code >= 400:
        detail = payload.get("message") or payload.get("error") or resp.text[:200]
        raise ApiPayError(f"ApiPay {method} {path} → {resp.status_code}: {detail}",
                          status_code=resp.status_code, payload=payload)
    return payload


def check_client(phone: str) -> dict:
    """POST /clients/check — does Kaspi know this number?

    Returns ``{"phone": "8XXXXXXXXXX", "has_kaspi": bool, "client_name": str|None}``
    (``client_name`` is "Имя Ф." when has_kaspi, else null).

    Runs BEFORE `create_invoice` on purpose. An invoice raised for a number
    Kaspi does not know is accepted by `POST /invoices` and only fails later,
    asynchronously, as a webhook — by then the HTTP request that could have
    told the client (or the manager) what went wrong is long finished, and the
    dead invoice has already been counted against the account's creation quota.
    This check moves both problems into the same request: no quota is spent and
    the caller can answer immediately.
    """
    return _request("POST", "/clients/check", {"phone": normalize_phone(phone)})


def create_invoice(phone: str, amount: int, description: str,
                   external_order_id: str) -> dict:
    """POST /invoices — ask the client to pay `amount` ₸ from their Kaspi app.

    `amount` must be whole tenge (1…99 999 999). Returns the ApiPay invoice
    object (``id``, ``status``, ``amount``, ``phone``, ``created_at``).
    """
    amount = int(amount)
    if not 1 <= amount <= 99_999_999:
        raise ApiPayError(f"Сумма вне допустимого диапазона ApiPay: {amount}")
    return _request("POST", "/invoices", {
        "phone_number": normalize_phone(phone),
        "amount": amount,
        "description": description[:255],
        "external_order_id": external_order_id,
    })


def get_invoice(invoice_id: int) -> dict:
    """GET /invoices/{id} — authoritative status, used to reconcile."""
    return _request("GET", f"/invoices/{int(invoice_id)}")


def cancel_invoice(invoice_id: int) -> dict:
    """POST /invoices/{id}/cancel — used when the reservation TTL releases the slot."""
    return _request("POST", f"/invoices/{int(invoice_id)}/cancel")


def verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    """HMAC-SHA256 over the RAW request body, compared in constant time.

    The header is ``X-Webhook-Signature: sha256=<hex>``. Must be the raw bytes
    Flask received — re-serialising the parsed JSON changes the digest.
    """
    secret = config.APIPAY_WEBHOOK_SECRET
    if not secret:
        logger.error("[APIPAY] APIPAY_WEBHOOK_SECRET не задан — вебхук отклонён.")
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")
