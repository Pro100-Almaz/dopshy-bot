"""Booking state → display-label maps.

Kept dependency-free (no project imports) so any layer — the sheet writer,
the service layer, the history renderer — can import these without risking a
circular import.
"""

# DB state → sheet status label (uppercase, matches Apps Script dropdown).
STATE_DISPLAY = {
    "draft":            "DRAFT",
    "awaiting_payment": "AWAITING_PAYMENT",
    "confirmed":        "CONFIRMED",
    "cancelled":        "CANCELLED",
    "failed":           "FAILED",
    "unpaid":           "UNPAID",
}

# DB state → Russian label (uppercase).
STATES_RUSSIAN = {
    "draft":            "ЧЕРНОВИК",
    "awaiting_payment": "ОЖИДАЕТ ОПЛАТЫ",
    "confirmed":        "ПОДТВЕРЖДЕНО",
    "cancelled":        "ОТМЕНЕНО",
    "failed":           "ПРОВАЛИЛОСЬ",
    "unpaid":           "НЕ ОПЛАЧЕНО",
}
