# Arena Document Extraction Data API

This repository is the bot/data service. PDF generation is handled by the
separate backend. That backend should call this endpoint to fetch complete,
report-ready Arena data.

## Endpoint

`POST /api/manager/documents/extract-data`

Headers:

```http
Authorization: Bearer <manager-token>
Content-Type: application/json
Accept: application/json
```

`X-API-Key: <manager-token>` is also accepted because this service uses the
existing manager API authentication middleware.

Request body:

```json
{
  "bot_type": "arena",
  "start_date": "2026-08-01",
  "end_date": "2026-08-31"
}
```

Validation:

- `bot_type` must be `arena`.
- `start_date` and `end_date` must be `YYYY-MM-DD`.
- `start_date <= end_date`.
- Range length must be at most 366 days.

Unsupported document types return:

```json
{
  "detail": "Only arena document extraction is supported now."
}
```

## Included Rows

Only final/reportable booking statuses are included:

```python
REPORT_INCLUDED_STATUSES = {"confirmed", "completed"}
```

The query is report-specific and not paginated. It returns every included
booking in the requested period with non-empty `field`, `date`, `time_start`,
and `time_end`, ordered by `date`, `time_start`, `field`, and `id`.

The database normally stores completed legacy bookings as `confirmed`, but
`completed` is still accepted for historical rows. After the repository query
returns, the service applies a second case-insensitive status filter so rows
outside `confirmed`/`completed` cannot leak into the document payload.

Fields are loaded from the active `fields` table. `active_field_count` is:

1. the count of active field rows when configured;
2. otherwise the count of distinct booking `field` values in the extract;
3. otherwise the configured `BOOKING_FIELDS` count, with a minimum of `1`.

## Success Response

```json
{
  "ok": true,
  "data": {
    "bot_type": "arena",
    "bot": "Arena",
    "period": {
      "start_date": "2026-08-01",
      "end_date": "2026-08-31",
      "days": 31
    },
    "included_statuses": ["completed", "confirmed"],
    "active_field_count": 3,
    "generated_at": "2026-08-26T12:30:00+05:00",
    "summary": {
      "total_paid_amount": 47000,
      "booking_count": 2,
      "total_booked_hours": 3,
      "load_level": 0.0013,
      "load_level_percent": 0.13,
      "per_field_load": [
        {
          "field": 1,
          "booked_hours": 2,
          "load_level": 0.0027,
          "load_level_percent": 0.27
        }
      ]
    },
    "bookings": [
      {
        "id": 1,
        "customer_name": "Aruzhan",
        "field": 1,
        "date": "2026-08-03",
        "time_start": "10:00",
        "time_end": "12:00",
        "duration_hours": 2,
        "price_total": 40000,
        "paid_cash": 10000,
        "paid_kaspi_qr": 0,
        "paid_api": 25000,
        "paid_avans": 5000,
        "total_paid_amount": 40000,
        "state": "confirmed",
        "source": "manager"
      }
    ]
  }
}
```

## Summary Rules

Payment total per booking:

```text
paid_cash + paid_kaspi_qr + paid_api + paid_avans
```

Null and empty payment values are treated as `0`.

Load level:

```text
period_days = (end_date - start_date).days + 1
load_level = total_booked_hours / (24 * period_days * active_field_count)
per_field_load = field_booked_hours / (24 * period_days)
```

`paid_api` is the existing accepted remote payment total from the `payments`
table, aggregated by `booking_id`. Multiple accepted payments on one booking
are summed. Payments without `booking_id` are ignored. Invalid numeric payment
values are treated as `0`.

Time and numeric normalization:

- `date` values are emitted as `YYYY-MM-DD`.
- `time_start` and `time_end` values are emitted as `HH:MM`.
- `23:59` end times count as `24:00` for duration and load calculations.
- Bookings crossing midnight are treated as ending on the next day.
- Money and hour values are rounded to two decimal places.
- Ratio values are rounded to four decimal places; percent values are rounded
  to two decimal places.

`generated_at` is emitted in `BOOKING_TIMEZONE`.
