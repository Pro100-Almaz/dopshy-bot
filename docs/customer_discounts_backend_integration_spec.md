# Customer and Discount Backend Integration Specification

This document describes the customer and discount functionality implemented in
the bot repository and the integration contract for the proxy backend.

## 1. Responsibilities

The bot service now owns:

- Registered customer persistence.
- Discount persistence and lifecycle state.
- Discount usage counters.
- Validation that a discount belongs to the booking customer.
- Atomic discount consumption during booking creation.
- Discount replacement and removal during booking editing.
- Customer and discount audit-history entries.
- Unified customer-list aggregation.
- Digits-only phone normalization.

The proxy backend remains responsible for:

- Authentication and RBAC.
- Determining the current staff member.
- Allowing only permitted roles to create discount requests.
- Allowing only super admins to approve or reject discounts.
- Supplying a trusted actor identity to the bot service.
- Preventing clients from spoofing `source`, `created_by`, or approval metadata.

All manager-service endpoints require the existing service authentication:

```http
Authorization: Bearer <service-token>
```

or:

```http
X-API-Key: <service-token>
```

Successful responses generally use:

```json
{
  "ok": true,
  "data": {}
}
```

Errors generally use:

```json
{
  "ok": false,
  "code": "ERROR_CODE",
  "message": "Human-readable message"
}
```

## 2. Database models

### 2.1 Customer

Table: `customers`

| Field | Type | Required | Description |
|---|---|---:|---|
| `id` | bigint | Yes | Primary key |
| `name` | string/null | No | Customer name |
| `phone` | string | Yes | Unique, digits-only phone |
| `is_regular_customer` | boolean | Yes | Defaults to `false` |
| `created_at` | datetime | Yes | Creation timestamp |
| `updated_at` | datetime | Yes | Last update timestamp |

Phone normalization removes every non-digit character:

```text
+7 (707) 111-22-33 -> 77071112233
```

It does not convert Kazakhstan numbers beginning with `8` into `7`. Therefore,
`87071112233` and `77071112233` are currently different numbers. The proxy
should send a consistent country-code format or rely on the bot service's
digits-only normalization.

### 2.2 Discount

Table: `discounts`

| Field | Type | Required | Description |
|---|---|---:|---|
| `id` | bigint | Yes | Primary key |
| `customer_id` | bigint | Yes | Foreign key to `customers.id` |
| `discount_amount` | decimal | Yes | Fixed nominal discount in tenge |
| `condition` | string/null | No | Optional condition or description |
| `status` | enum | Yes | `pending`, `approved`, or `rejected` |
| `usage_limit` | integer | Yes | Maximum uses; defaults to `5` |
| `usages_left` | integer | Yes | Remaining uses |
| `is_active` | boolean | Yes | Whether the discount can currently be assigned |
| `created_by` | string/null | No | Actor who created the request |
| `approved_by` | string/null | No | Actor who approved the request |
| `approved_at` | datetime/null | No | Approval timestamp |
| `created_at` | datetime | Yes | Creation timestamp |
| `updated_at` | datetime | Yes | Last update timestamp |

API responses also contain:

```json
{
  "usages_count": 2,
  "customer_name": "Алия",
  "customer_phone": "77071112233"
}
```

`usages_count` is calculated as:

```text
usage_limit - usages_left
```

### 2.3 Booking additions

The `bookings` table now contains:

| Field | Type | Description |
|---|---|---|
| `customer_id` | bigint | Required FK to the registered customer |
| `discount_id` | bigint/null | Selected discount |
| `discount_amount` | decimal | Snapshot of the nominal discount amount |
| `price_before_discount` | decimal/null | Original gross booking price |
| `price_total` | decimal | Final price after discount |

Price calculation:

```text
price_total = max(price_before_discount - discount_amount, 0)
```

For a gross price of `13,500` and a discount of `20,000`, the booking stores:

```json
{
  "discount_amount": 20000,
  "price_before_discount": 13500,
  "price_total": 0
}
```

The nominal discount is retained so the UI can display the coupon that was
selected even when it exceeds the booking price.

Every booking must have `customer_id`. The booking API accepts an explicit
customer ID; when only a phone is provided, the service resolves or creates the
customer by normalized phone. Requests containing neither value are rejected.
The database column is `NOT NULL`, and customer deletion is restricted while
bookings reference that customer.

`is_registered` is not stored on `customers`. It is derived by the unified
contacts endpoint: a matching PostgreSQL customer means `true`; a contact that
exists only in SQLite means `false`.

## 3. Required RBAC in the proxy backend

The bot service intentionally does not enforce staff roles.

| Operation | Manager | Senior admin/admin | Super admin |
|---|---:|---:|---:|
| View customers | Yes | Yes | Yes |
| Create customer | Yes | Yes | Yes |
| Update regular-customer flag | Product policy | Yes | Yes |
| View discounts | Yes | Yes | Yes |
| Assign an approved discount | Yes | Yes | Yes |
| Create a discount request | No, unless desired | Yes | Yes |
| Create an already-approved discount | No | No | Yes |
| Approve or reject a discount | No | No | Yes |
| Modify or deactivate a discount | No | Product policy | Yes |

For an ordinary admin creating a discount, the proxy must force:

```json
{
  "status": "pending"
}
```

The proxy should populate `source` from the authenticated user:

```json
{
  "source": "admin@example.com"
}
```

It must not trust a `source` value supplied directly by the frontend.

## 4. Customer API

### 4.1 Find customer by phone

```http
GET /api/manager/customers?phone=+7%20707%20111%2022%2033
```

Successful match:

```json
{
  "ok": true,
  "data": {
    "id": 1,
    "name": "Алия",
    "phone": "77071112233",
    "is_regular_customer": true,
    "created_at": "2026-09-19T10:00:00+00:00",
    "updated_at": "2026-09-19T10:00:00+00:00"
  }
}
```

No matching customer:

```json
{
  "ok": true,
  "data": null
}
```

This endpoint should be used by the booking modal's phone-check action.

### 4.2 List customers

```http
GET /api/manager/customers
GET /api/manager/customers?search=Алия
GET /api/manager/customers?search=7707
```

Response:

```json
{
  "ok": true,
  "data": [
    {
      "id": 1,
      "name": "Алия",
      "phone": "77071112233",
      "is_regular_customer": true,
      "created_at": "2026-09-19T10:00:00+00:00",
      "updated_at": "2026-09-19T10:00:00+00:00"
    }
  ]
}
```

### 4.3 Get customer

```http
GET /api/manager/customers/{customer_id}
```

Not found, HTTP `404`:

```json
{
  "ok": false,
  "code": "NOT_FOUND",
  "message": "Клиент не найден."
}
```

### 4.4 Create customer

```http
POST /api/manager/customers
Content-Type: application/json
```

Request:

```json
{
  "name": "Алия",
  "phone": "+7 (707) 111-22-33",
  "is_regular_customer": false,
  "source": "admin@example.com"
}
```

Only `phone` is required. `name` may be absent or empty.

Response: HTTP `201`.

If the normalized phone already belongs to a customer row, this request returns
and updates that same row. It does not create a duplicate ID.

### 4.5 Update customer

```http
PATCH /api/manager/customers/{customer_id}
```

All fields are optional:

```json
{
  "name": "Новое имя",
  "phone": "+7 707 999 88 77",
  "is_regular_customer": true,
  "source": "admin@example.com"
}
```

### 4.6 Delete customer

```http
DELETE /api/manager/customers/{customer_id}
```

Customers with existing discounts cannot be deleted. The response is HTTP
`409`:

```json
{
  "ok": false,
  "code": "CUSTOMER_IN_USE",
  "message": "Нельзя удалить клиента со скидками."
}
```

## 5. Unified contacts API

The existing endpoint remains:

```http
GET /api/manager/contacts
```

It merges:

- WhatsApp contacts from SQLite.
- Customers found in bookings.
- Registered customers from PostgreSQL.

Deduplication uses the digits-only phone. Each arena customer row now includes:

```json
{
  "phone": "77071112233",
  "name": "Алия",
  "texted": true,
  "has_booking": true,
  "is_registered": true,
  "is_regular_customer": true,
  "last_activity": "2026-09-19T15:00:00+05:00",
  "paused": false,
  "paused_reason": null
}
```

Rules:

- SQLite/conversation only: both new flags are `false`.
- Booking only: `has_booking=true`, `is_registered=false`, and
  `is_regular_customer=false`.
- PostgreSQL customer only: `is_registered=true`, `has_booking=false`, and the
  regular-customer value comes from PostgreSQL.
- Booking plus PostgreSQL customer: both `has_booking` and `is_registered` are
  true, and the regular-customer value comes from PostgreSQL.

The frontend should hide the regular-customer switch for unregistered rows.

Pagination remains optional:

```http
GET /api/manager/contacts?page=1&page_size=20
```

Paginated response:

```json
{
  "ok": true,
  "data": [],
  "page": 1,
  "page_size": 20,
  "total": 0,
  "total_pages": 0
}
```

Without pagination parameters, the endpoint retains its legacy bare-array
response.

## 6. Discount API

### 6.1 List discounts

```http
GET /api/manager/discounts
GET /api/manager/discounts?customer_id=1
GET /api/manager/discounts?phone=77071112233
GET /api/manager/discounts?status=pending
GET /api/manager/discounts?available_only=true
```

Filters can be combined:

```http
GET /api/manager/discounts?phone=77071112233&available_only=true
```

`available_only=true` means:

```text
status = approved
is_active = true
usages_left > 0
```

Example response:

```json
{
  "ok": true,
  "data": [
    {
      "id": 10,
      "customer_id": 1,
      "customer_name": "Алия",
      "customer_phone": "77071112233",
      "discount_amount": 10000,
      "condition": "Для постоянного клиента",
      "status": "approved",
      "usage_limit": 5,
      "usages_left": 3,
      "usages_count": 2,
      "is_active": true,
      "created_by": "admin@example.com",
      "approved_by": "superadmin@example.com",
      "created_at": "2026-09-19T10:00:00+00:00",
      "updated_at": "2026-09-19T11:00:00+00:00",
      "approved_at": "2026-09-19T11:00:00+00:00"
    }
  ]
}
```

For booking creation and editing, fetch available discounts using:

```http
GET /api/manager/discounts?phone={normalized_phone}&available_only=true
```

### 6.2 Get discount

```http
GET /api/manager/discounts/{discount_id}
```

### 6.3 Create discount request

```http
POST /api/manager/discounts
```

Request:

```json
{
  "customer_id": 1,
  "discount_amount": 10000,
  "condition": "Для постоянного клиента",
  "status": "pending",
  "usage_limit": 5,
  "source": "admin@example.com"
}
```

Required fields:

- `customer_id`
- `discount_amount`

Defaults:

```json
{
  "status": "pending",
  "usage_limit": 5
}
```

For a non-super-admin, the proxy must ignore a submitted status and send
`pending`. A super admin may create the discount directly as `approved`.

### 6.4 Approve discount

```http
PATCH /api/manager/discounts/{discount_id}
```

```json
{
  "status": "approved",
  "source": "superadmin@example.com"
}
```

Approval performs the following changes:

- `status` becomes `approved`.
- `is_active` becomes true when `usages_left > 0`.
- `approved_by` becomes the trusted `source`.
- `approved_at` is set.

Only a super-admin proxy route should permit this operation.

### 6.5 Reject discount

```http
PATCH /api/manager/discounts/{discount_id}
```

```json
{
  "status": "rejected",
  "source": "superadmin@example.com"
}
```

Rejection sets `is_active=false` and clears `approved_by`.

The API also accepts lowercase `canceled` as an alias and persists it as
`rejected`. The canonical API response is `rejected`.

### 6.6 Edit discount

Supported patch fields:

```json
{
  "discount_amount": 15000,
  "condition": "Updated condition",
  "status": "approved",
  "is_active": true,
  "usage_limit": 10,
  "source": "superadmin@example.com"
}
```

Rules:

- `discount_amount` must be greater than zero.
- `usage_limit` must be greater than zero.
- `usage_limit` cannot be reduced below `usages_count`.
- Increasing `usage_limit` increases `usages_left` while preserving the number
  already used.
- Only approved discounts may be active.
- A discount at zero uses cannot be reactivated unless its limit is increased.
- Changing the amount does not alter historical bookings because bookings keep
  a snapshot.

### 6.7 Delete discount

```http
DELETE /api/manager/discounts/{discount_id}
```

A discount can only be deleted if it has never been used:

```text
usages_left = usage_limit
```

Otherwise the response is HTTP `409`:

```json
{
  "ok": false,
  "code": "DISCOUNT_IN_USE",
  "message": "Скидка не найдена или уже использовалась."
}
```

## 7. Single booking creation with a discount

Endpoint:

```http
POST /api/manager/bookings
```

New request fields are `discount_id` and `prepayment`:

```json
{
  "field": 1,
  "date": "2026-10-01",
  "time_start": "10:00",
  "time_end": "11:00",
  "customer": "Алия",
  "customer_id": 1,
  "phone": "+7 707 111 22 33",
  "price_total": 13500,
  "discount_id": 10,
  "prepayment": 3500
}
```

During creation, incoming `price_total` is treated as the gross price. The
resulting booking contains:

```json
{
  "phone": "77071112233",
  "customer_id": 1,
  "discount_id": 10,
  "discount_amount": 10000,
  "price_before_discount": 13500,
  "price_total": 3500,
  "paid_avans": 3500
}
```

The discount consumes one use in the same database transaction as booking
creation.

If `customer_id` is provided, its stored phone must match the normalized
booking phone. An explicit customer ID may be used without `phone`; in that
case the booking phone is populated from the customer record.

If only `phone` is provided, the service upserts a customer record and stores
its ID on the booking. If neither `customer_id` nor `phone` is supplied, the
service returns `CUSTOMER_REQUIRED`.

### Discount validation

A discount can only be applied if:

```text
discount exists
discount.status = approved
discount.is_active = true
discount.usages_left > 0
discount.customer.phone = normalized booking phone
```

The check and decrement use a row lock, preventing concurrent requests from
both consuming the final use.

### Discount errors

Not found:

```json
{
  "ok": false,
  "code": "DISCOUNT_NOT_FOUND",
  "message": "Скидка не найдена."
}
```

Wrong customer:

```json
{
  "ok": false,
  "code": "DISCOUNT_CUSTOMER_MISMATCH",
  "message": "Скидка принадлежит другому клиенту."
}
```

Pending, rejected, inactive, or exhausted:

```json
{
  "ok": false,
  "code": "DISCOUNT_UNAVAILABLE",
  "message": "Скидка недоступна или закончилась."
}
```

Invalid ID:

```json
{
  "ok": false,
  "code": "INVALID_DISCOUNT",
  "message": "discount_id должен быть числом."
}
```

Missing customer identity:

```json
{
  "ok": false,
  "code": "CUSTOMER_REQUIRED",
  "message": "Для брони обязателен customer_id или phone."
}
```

Unknown or mismatched explicit customer IDs use `CUSTOMER_NOT_FOUND`,
`INVALID_CUSTOMER`, or `CUSTOMER_PHONE_MISMATCH`.

The existing single-booking route currently returns HTTP `409` for booking
service failures, including discount failures. The proxy should map the
machine-readable `code`, not only the HTTP status.

## 8. Batch booking with discounts

Endpoint:

```http
POST /api/manager/bookings/batch
```

A top-level discount acts as the default for every slot:

```json
{
  "customer": "Алия",
  "customer_id": 1,
  "phone": "77071112233",
  "discount_id": 10,
  "prepayment": 3500,
  "slots": [
    {
      "field": 1,
      "date": "2026-10-01",
      "time_start": "10:00",
      "time_end": "11:00"
    },
    {
      "field": 1,
      "date": "2026-10-03",
      "time_start": "12:00",
      "time_end": "13:00"
    }
  ]
}
```

A slot can override the top-level discount:

```json
{
  "customer": "Алия",
  "phone": "77071112233",
  "slots": [
    {
      "field": 1,
      "date": "2026-10-01",
      "time_start": "10:00",
      "time_end": "11:00",
      "discount_id": 10
    },
    {
      "field": 2,
      "date": "2026-10-03",
      "time_start": "12:00",
      "time_end": "13:00",
      "discount_id": 11
    },
    {
      "field": 3,
      "date": "2026-10-05",
      "time_start": "16:00",
      "time_end": "17:00",
      "discount_id": null
    }
  ]
}
```

Precedence:

```text
slot.discount_id, when present
otherwise top-level discount_id
otherwise no discount
```

The batch is atomic. Either all bookings and discount decrements commit, or all
of them roll back. If a discount with only two uses is assigned to three slots,
no bookings are created and both original uses remain available.

Each generated recurring occurrence consumes one use. For a cross-midnight
logical booking represented by two database rows, only the primary half
consumes a use.

## 9. Booking edit and discount replacement

Endpoint:

```http
PATCH /api/manager/bookings/{booking_id}
```

### Apply or replace a discount

```json
{
  "discount_id": 10,
  "source": "manager@example.com"
}
```

When replacing a discount, the service atomically:

1. Returns one use to the old discount.
2. Validates and consumes one use from the new discount.
3. Recalculates the booking price.
4. Records the change in booking history.

If the new discount is invalid, the whole transaction rolls back and the old
discount remains assigned.

### Remove a discount

```json
{
  "discount_id": null,
  "source": "manager@example.com"
}
```

This operation:

- Returns one use to the previous discount.
- Clears `discount_id`.
- Sets `discount_amount` to zero.
- Restores `price_total` to the gross price.
- Clears `price_before_discount`.

### Change booking phone

The endpoint now accepts:

```json
{
  "phone": "+7 707 111 22 33"
}
```

If the booking has a discount, the normalized phone must match its owner.

### Reschedule a discounted booking

When date, time, duration, or field changes, the service recalculates the gross
price and reapplies the stored discount snapshot:

```text
new price_total = max(new gross price - stored discount_amount, 0)
```

Rescheduling does not consume another use.

## 10. Booking response additions

The following existing endpoints now return discount fields:

```http
GET /api/manager/bookings
GET /api/manager/bookings/all
GET /api/manager/bookings/{booking_id}
GET /api/manager/bookings/range/{start_date}/{end_date}
```

New fields:

```json
{
  "customer_id": 1,
  "discount_id": 10,
  "discount_amount": 10000,
  "price_before_discount": 13500,
  "price_total": 3500
}
```

The booking listing should display `discount_amount`. The edit modal should use
`discount_id` as the currently selected option.

## 11. Prepayment behavior

The bot backend persists supplied `prepayment` as `paid_avans`. It does not
automatically calculate the UI default.

The frontend should calculate:

```text
suggested prepayment = min(price_total after discount, 10000)
```

Examples:

```text
Gross: 13,500
Discount: 10,000
Final: 3,500
Suggested prepayment: 3,500
```

```text
Gross: 31,000
Discount: 10,000
Final: 21,000
Suggested prepayment: 10,000
```

```text
Gross: 13,500
Discount: 20,000
Final: 0
Suggested prepayment: 0
```

The prepayment field may remain editable. If ApiPay is enabled, its existing
configured advance-payment behavior may override manually supplied prepayment
for chargeable slots.

## 12. History integration

The existing endpoint remains:

```http
GET /api/manager/history
```

History rows now also contain `customer_id` and `discount_id`:

```json
{
  "booking_id": "",
  "customer_id": 1,
  "discount_id": 10,
  "source": "admin@example.com",
  "description": "Создана скидка 10000 тг со статусом pending.",
  "created_at": "2026-09-19T10:00:00+00:00"
}
```

Possible associations:

- Booking event: `booking_id` is populated.
- Customer event: `customer_id` is populated.
- Discount event: `customer_id` and `discount_id` are populated.

Recorded actions include:

- Customer created, updated, or deleted.
- Discount created, updated, approved, rejected, or deleted.
- Booking discount replaced or removed.

The existing serializer converts database nulls to empty strings. Unrelated
entity IDs may therefore be returned as `""` rather than JSON `null`. The proxy
should preferably normalize empty identifier strings to `null` in its public
API.

## 13. Suggested proxy workflow

### Booking modal step 1

1. Collect the phone number.
2. Call `GET /api/manager/customers?phone={phone}`.
3. If `data` is null, show the customer-creation action.
4. Create the customer through `POST /api/manager/customers`.
5. Store the returned customer ID and normalized phone.

### Booking modal step 2

Prefetch:

```http
GET /api/manager/discounts?phone={customer.phone}&available_only=true
```

Display the following per discount:

- `discount_amount`
- `usages_left`
- `condition`, when present

While assigning discounts to batch slots, maintain a local counter:

```text
locally available = fetched usages_left - assignments in the current form
```

The bot remains the final authority and revalidates the complete batch
atomically.

### Admin discount request

```json
{
  "customer_id": 1,
  "discount_amount": 10000,
  "condition": "Optional condition",
  "status": "pending",
  "source": "<authenticated admin>"
}
```

### Super-admin approval

```json
{
  "status": "approved",
  "source": "<authenticated super admin>"
}
```

### Super-admin rejection

```json
{
  "status": "rejected",
  "source": "<authenticated super admin>"
}

## 14. Relevant implementation files

- `migrations/052_customers_discounts.sql`
- `migrations/053_bookings_customer_id.sql`
- `integrations/repo/customer_discount_repo.py`
- `integrations/booking_service.py`
- `blueprints/manager_api.py`
- `integrations/repo/booking_repo.py`
- `integrations/repo/history_repo.py`
- `tests/test_discounts.py`

## 15. Verification

The implementation has focused coverage for:

- Phone normalization and duplicate rejection.
- Rejection of pending discounts during booking creation.
- Price flooring at zero when a discount exceeds the gross price.
- Automatic deactivation at zero remaining uses.
- Discount-owner phone validation.
- Per-slot batch discounts.
- Atomic rollback when a batch exceeds available uses.
- Discount replacement and removal during booking editing.
- Customer and discount history entries.

The focused PostgreSQL integration suite passes all eight discount/customer
tests.
