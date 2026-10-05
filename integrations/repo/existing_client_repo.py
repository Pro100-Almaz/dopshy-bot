"""Existing academy clients — numbers the academy bots must not auto-reply to.

A phone counts as an existing client when either:
  A. a manager marked one of its students as subscribed (academy_users), or
  B. it was imported into academy_existing_clients (pre-bot WhatsApp contacts).
"""
from integrations.repo.postgres import _conn
from integrations.repo.utils import normalize_phone


def canonical_kz_phone(phone: str | None) -> str:
    """Digits-only phone in the 7XXXXXXXXXX form WhatsApp delivers.

    Imported lists mix '+7 700 …', '8 700 …' and bare 10-digit numbers.
    """
    digits = normalize_phone(phone)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return digits


def is_existing_academy_client(phone: str) -> bool:
    key = canonical_kz_phone(phone)
    if not key:
        return False
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM academy_existing_clients WHERE phone = %(key)s
                ) OR EXISTS (
                    SELECT 1 FROM academy_users
                    WHERE subscribed
                      AND parent_phone IS NOT NULL
                      AND regexp_replace(
                              regexp_replace(parent_phone, '\\D', '', 'g'),
                              '^8(\\d{10})$', '7\\1'
                          ) = %(key)s
                )
                """,
                {"key": key},
            )
            return bool(cur.fetchone()[0])


def add_existing_clients(phones: list[str], source: str = "import") -> int:
    """Insert phones (any format); returns how many were new."""
    keys = sorted({k for k in (canonical_kz_phone(p) for p in phones) if len(k) == 11})
    if not keys:
        return 0
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO academy_existing_clients (phone, source)
                SELECT unnest(%s::text[]), %s
                ON CONFLICT (phone) DO NOTHING
                """,
                (keys, source),
            )
            return cur.rowcount
