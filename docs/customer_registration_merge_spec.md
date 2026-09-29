# Customer Registration Merge Specification

## Purpose

`GET /api/manager/contacts` returns one normalized customer list assembled from
two independent data sources:

1. The bot's SQLite contact/conversation data.
2. The PostgreSQL `customers` table, together with booking metadata where
   available.

`is_registered` is a response-only value. It is not a database column and must
never be selected from, inserted into, or updated on `customers`.

## Identity and deduplication

Records from both sources are matched by normalized phone number. Normalization
must remove formatting characters and produce the same digits-only key for both
sources before merging.

Two records represent the same customer when their normalized phone numbers
are equal. Names and raw phone formatting must not be used as identity keys.

## Required response fields

Every contact returned by `GET /api/manager/contacts` must include:

```json
{
  "phone": "77001234567",
  "name": "Customer name",
  "texted": true,
  "has_booking": true,
  "is_registered": true,
  "is_regular_customer": false,
  "last_activity": "2026-09-20T12:00:00+05:00",
  "paused": false,
  "paused_reason": null
}
```

## Derivation rules

The public flags must be computed after both sources have been merged.

| SQLite record | PostgreSQL `customers` record | `is_registered` | `is_regular_customer` |
|---|---|---:|---:|
| Yes | No | `false` | `false` |
| No | Yes | `true` | Value stored in PostgreSQL |
| Yes | Yes | `true` | Value stored in PostgreSQL |
| No | No | Not returned | Not returned |

Detailed behavior:

1. SQLite only: the person is known to the bot but is not registered. Always
   return `is_registered=false` and `is_regular_customer=false`.
2. PostgreSQL only: the customer was explicitly created but has no corresponding
   SQLite record yet. Return `is_registered=true`; return the persisted
   `customers.is_regular_customer` value.
3. Both sources: merge into one row by normalized phone. Return
   `is_registered=true`; return the persisted PostgreSQL regular-customer value.
4. A booking row by itself does not prove registration. Registration requires a
   matching row in the PostgreSQL `customers` table.

The PostgreSQL regular-customer flag is authoritative. SQLite must never set or
override it.

## Backend query contract

The PostgreSQL query used by the contacts endpoint must expose customer
presence explicitly, preferably as nullable `customer_id`:

```sql
SELECT
    COALESCE(c.phone, b.phone) AS phone,
    COALESCE(c.name, b.customer_name) AS customer_name,
    (b.phone IS NOT NULL) AS has_booking,
    c.id AS customer_id,
    COALESCE(c.is_regular_customer, FALSE) AS is_regular_customer
FROM booked b
FULL OUTER JOIN customers c ON c.phone = b.phone;
```

The query must not reference `c.is_registered`. That column does not exist by
design.

## Merge algorithm

For each normalized phone, the endpoint should maintain internal source state:

```text
exists_in_sqlite
exists_in_postgres
postgres_is_regular_customer
```

After reading both sources:

```text
is_registered = exists_in_postgres
is_regular_customer = (
    postgres_is_regular_customer if exists_in_postgres else false
)
```

Internal source markers must not appear in the JSON response.

## Error behavior

All manager API responses, including failures, must be JSON. A schema mismatch
must return an appropriate JSON 5xx response and must not expose an HTML Flask
error page to the proxy/frontend.

## Acceptance criteria

1. A phone found only in SQLite is returned once with both flags set to `false`.
2. A phone found only in `customers` is returned once with
   `is_registered=true` and its stored regular-customer value.
3. A phone found in both sources is returned once with PostgreSQL controlling
   the two customer-state flags.
4. Differently formatted versions of the same phone merge into one response.
5. A booking without a matching `customers` row does not become registered.
6. No migration adds an `is_registered` column.
7. No SQL statement references `customers.is_registered`.
8. Private merge markers are absent from the serialized response.
9. The endpoint continues to support its existing pagination and `bot_type`
   filtering behavior.

## Proxy/frontend expectations

The proxy backend should pass through `is_registered` and
`is_regular_customer` without re-deriving them. The frontend should hide the
regular-customer switch when `is_registered=false`; otherwise it should display
the value of `is_regular_customer`.
