from integrations.repo import postgres


class BaseDraftHandler:
    def __init__(self, bot_name: str):
        self.BOT_NAME = bot_name

    def update_draft_in_db(self, booking_id: int | None, data: dict) -> None:
        """Persist the merged data back to the draft row in PostgreSQL."""
        if booking_id is None:
            return

        update_fields: dict = {}
        for key in ("date", "time_start", "time_end", "field",
                     "players", "customer_name", "format"):
            if data.get(key) is not None:
                update_fields[key] = data[key]

        if update_fields:
            postgres.update_draft(self.BOT_NAME, booking_id, **update_fields)
