class BaseHelper:
    _CANCEL_PHRASES = (
        "отмен", "стоп", "передум", "не хочу", "не нужно", "не надо",
        "тоқтат", "керек емес", "бас тарт",
    )

    @staticmethod
    def is_ready_for_confirm(draft: dict) -> bool:
        """True when all 6 booking fields are filled in the draft."""
        return all([
            draft.get("date"),
            draft.get("time_start"),
            draft.get("time_end"),
            draft.get("field"),
            draft.get("players"),
            draft.get("customer_name"),
        ])

    def is_cancel_intent(self, text: str) -> bool:
        lower = text.lower()
        return any(p in lower for p in self._CANCEL_PHRASES)

    @staticmethod
    def draft_to_data(draft: dict) -> dict:
        """Convert a DB draft row into the standardized data dict used everywhere."""
        return {
            "date": str(draft["date"]) if draft.get("date") else None,
            "time_start": (
                str(draft["time_start"])[:5] if draft.get("time_start") else None
            ),
            "time_end": (
                str(draft["time_end"])[:5] if draft.get("time_end") else None
            ),
            "field": int(draft["field"]) if draft.get("field") else None,
            "format": draft.get("format"),
            "players": int(draft["players"]) if draft.get("players") else None,
            "customer_name": draft.get("customer_name"),
            "booking_id": draft["id"],
            "client_token": str(draft.get("client_token", "")),
        }

    @staticmethod
    def merge_data(current: dict, extracted: dict) -> dict:
        """
        Merge newly extracted values into the current draft data.
        Only non-null extracted values overwrite existing ones.
        """
        merged = dict(current)

        field_map = {
            "date": "date",
            "time_start": "time_start",
            "time_end": "time_end",
            "field": "field",
            "format": "format",
            "players": "players",
            "customer_name": "customer_name",
            "name": "customer_name",
            "lang": "lang",
        }
        for ext_key, data_key in field_map.items():
            val = extracted.get(ext_key)
            if val is not None:
                merged[data_key] = val

        return merged
