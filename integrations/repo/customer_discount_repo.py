"""Persistence helpers for registered customers and their discounts."""

from decimal import Decimal

import psycopg2
import psycopg2.extras

from integrations.repo.utils import _conn, normalize_phone


DISCOUNT_STATUSES = {"pending", "approved", "rejected"}


def _row(cur) -> dict | None:
    value = cur.fetchone()
    return dict(value) if value else None


def _record_admin_history(cur, source: str, description: str, *,
                          customer_id: int | None = None,
                          discount_id: int | None = None) -> None:
    cur.execute(
        """INSERT INTO booking_history
               (booking_id, customer_id, discount_id, source, description)
           VALUES (NULL, %s, %s, %s, %s)""",
        (customer_id, discount_id, source, description),
    )


def get_customer(customer_id: int) -> dict | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM customers WHERE id = %s", (customer_id,))
            return _row(cur)


def get_customer_by_phone(phone: str) -> dict | None:
    key = normalize_phone(phone)
    if not key:
        return None
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM customers WHERE phone = %s", (key,))
            return _row(cur)


def list_customers(search: str | None = None) -> list[dict]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if search:
                like = f"%{search.strip()}%"
                phone_like = f"%{normalize_phone(search)}%"
                cur.execute(
                    """SELECT * FROM customers
                       WHERE name ILIKE %s OR phone LIKE %s
                       ORDER BY updated_at DESC, id DESC""",
                    (like, phone_like),
                )
            else:
                cur.execute("SELECT * FROM customers ORDER BY updated_at DESC, id DESC")
            return [dict(row) for row in cur.fetchall()]


def create_customer(name: str | None, phone: str, is_regular_customer: bool = False,
                    source: str = "manager") -> dict:
    key = normalize_phone(phone)
    if not key:
        raise ValueError("phone is required")
    clean_name = (name or "").strip() or None
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """INSERT INTO customers (name, phone, is_regular_customer)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (phone) DO UPDATE SET
                       name = COALESCE(EXCLUDED.name, customers.name),
                       is_regular_customer = (customers.is_regular_customer
                                              OR EXCLUDED.is_regular_customer),
                       updated_at = NOW()
                   RETURNING *""",
                (clean_name, key, bool(is_regular_customer)),
            )
            row = dict(cur.fetchone())
            _record_admin_history(
                cur, source, f"Создан клиент {clean_name or key}.", customer_id=row["id"]
            )
            return row


def update_customer(customer_id: int, *, name=None, phone=None,
                    is_regular_customer=None, source: str = "manager") -> dict | None:
    changes = {}
    if name is not None:
        changes["name"] = str(name).strip() or None
    if phone is not None:
        key = normalize_phone(phone)
        if not key:
            raise ValueError("phone is required")
        changes["phone"] = key
    if is_regular_customer is not None:
        changes["is_regular_customer"] = bool(is_regular_customer)
    if not changes:
        return get_customer(customer_id)
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            values = list(changes.values()) + [customer_id]
            cur.execute(
                f"UPDATE customers SET {', '.join(f'{key} = %s' for key in changes)}, "
                "updated_at = NOW() WHERE id = %s RETURNING *",
                values,
            )
            row = _row(cur)
            if row:
                if "phone" in changes:
                    cur.execute(
                        "UPDATE bookings SET phone = %s WHERE customer_id = %s",
                        (changes["phone"], customer_id),
                    )
                _record_admin_history(
                    cur, source, "Изменены данные клиента.", customer_id=customer_id
                )
            return row


def delete_customer(customer_id: int, source: str = "manager") -> bool:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name, phone FROM customers WHERE id = %s", (customer_id,))
            old = cur.fetchone()
            if not old:
                return False
            # History keeps the human-readable snapshot; its FK is SET NULL.
            _record_admin_history(
                cur, source, f"Удалён клиент {old[0] or old[1]}.", customer_id=customer_id
            )
            cur.execute("DELETE FROM customers WHERE id = %s", (customer_id,))
            return True


_DISCOUNT_SELECT = """
    SELECT d.*, c.name AS customer_name, c.phone AS customer_phone,
           (d.usage_limit - d.usages_left) AS usages_count
    FROM discounts d
    JOIN customers c ON c.id = d.customer_id
"""


def get_discount(discount_id: int) -> dict | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_DISCOUNT_SELECT + " WHERE d.id = %s", (discount_id,))
            return _row(cur)


def list_discounts(*, customer_id: int | None = None, phone: str | None = None,
                   status: str | None = None, available_only: bool = False) -> list[dict]:
    clauses, params = [], []
    if customer_id is not None:
        clauses.append("d.customer_id = %s")
        params.append(customer_id)
    if phone is not None:
        clauses.append("c.phone = %s")
        params.append(normalize_phone(phone))
    if status is not None:
        clauses.append("d.status = %s")
        params.append(status)
    if available_only:
        clauses.extend(("d.status = 'approved'", "d.is_active", "d.usages_left > 0"))
    query = _DISCOUNT_SELECT
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY d.created_at DESC, d.id DESC"
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]


def create_discount(customer_id: int, discount_amount, *, condition: str | None = None,
                    status: str = "pending", usage_limit: int = 5,
                    source: str = "manager") -> dict:
    if status not in DISCOUNT_STATUSES:
        raise ValueError("invalid discount status")
    amount = Decimal(str(discount_amount))
    limit = int(usage_limit)
    if amount <= 0 or limit <= 0:
        raise ValueError("discount_amount and usage_limit must be positive")
    active = status == "approved"
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """INSERT INTO discounts
                       (customer_id, discount_amount, condition, status,
                        usage_limit, usages_left, is_active, created_by,
                        approved_by, approved_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                           CASE WHEN %s THEN %s ELSE NULL END,
                           CASE WHEN %s THEN NOW() ELSE NULL END)
                   RETURNING id""",
                (customer_id, amount, condition, status, limit, limit, active, source,
                 active, source, active),
            )
            discount_id = cur.fetchone()["id"]
            _record_admin_history(
                cur, source, f"Создана скидка {amount} тг со статусом {status}.",
                customer_id=customer_id, discount_id=discount_id,
            )
    return get_discount(discount_id)


def update_discount(discount_id: int, *, discount_amount=None, condition=None,
                    status=None, is_active=None, usage_limit=None,
                    source: str = "manager") -> dict | None:
    if status is not None and status not in DISCOUNT_STATUSES:
        raise ValueError("invalid discount status")
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM discounts WHERE id = %s FOR UPDATE", (discount_id,))
            old = _row(cur)
            if not old:
                return None
            used = old["usage_limit"] - old["usages_left"]
            changes = {}
            if discount_amount is not None:
                amount = Decimal(str(discount_amount))
                if amount <= 0:
                    raise ValueError("discount_amount must be positive")
                changes["discount_amount"] = amount
            if condition is not None:
                changes["condition"] = condition
            if usage_limit is not None:
                limit = int(usage_limit)
                if limit <= 0 or limit < used:
                    raise ValueError("usage_limit cannot be below usages_count")
                changes["usage_limit"] = limit
                changes["usages_left"] = limit - used
            if status is not None:
                changes["status"] = status
                changes["is_active"] = status == "approved" and old["usages_left"] > 0
                changes["approved_by"] = source if status == "approved" else None
            if is_active is not None:
                if bool(is_active) and (status or old["status"]) != "approved":
                    raise ValueError("only approved discounts can be active")
                changes["is_active"] = bool(is_active) and old["usages_left"] > 0
            if not changes:
                return get_discount(discount_id)
            sets = [f"{key} = %s" for key in changes]
            values = list(changes.values())
            if status == "approved":
                sets.append("approved_at = NOW()")
            sets.append("updated_at = NOW()")
            values.append(discount_id)
            cur.execute(
                f"UPDATE discounts SET {', '.join(sets)} WHERE id = %s RETURNING customer_id",
                values,
            )
            customer_id = cur.fetchone()["customer_id"]
            _record_admin_history(
                cur, source, f"Изменена скидка: статус {status or old['status']}.",
                customer_id=customer_id, discount_id=discount_id,
            )
    return get_discount(discount_id)


def delete_discount(discount_id: int, source: str = "manager") -> bool:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT customer_id FROM discounts WHERE id = %s AND usages_left = usage_limit",
                (discount_id,),
            )
            row = cur.fetchone()
            if not row:
                return False
            _record_admin_history(
                cur, source, "Удалена неиспользованная скидка.",
                customer_id=row[0], discount_id=discount_id,
            )
            cur.execute("DELETE FROM discounts WHERE id = %s", (discount_id,))
            return cur.rowcount == 1
