import json


class BaseButton:
    @staticmethod
    def get_buttons(text: str, buttons_list: list[str]) -> str:
        """Build a WhatsApp interactive button message JSON string."""
        payload = {
            "type": "button",
            "body": {"text": text},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {"id": f"id_{i}", "title": btn},
                    }
                    for i, btn in enumerate(buttons_list, start=1)
                ],
            },
        }
        return json.dumps(payload)
