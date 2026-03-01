"""Meta WhatsApp Cloud API client — send messages."""

import requests
import config


def send_text_message(to: str, text: str) -> dict:
    """
    Send a plain-text WhatsApp message.

    Args:
        to: Recipient's phone number in international format (e.g. "77001234567")
           or a group JID.
        text: Message body.
    Returns:
        API response JSON.
    """
    headers = {
        "Authorization": f"Bearer {config.WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    response = requests.post(config.WHATSAPP_API_URL, json=payload, headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()


def mark_as_read(message_id: str) -> None:
    """Mark an incoming message as read (shows blue ticks)."""
    headers = {
        "Authorization": f"Bearer {config.WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    try:
        requests.post(config.WHATSAPP_API_URL, json=payload, headers=headers, timeout=5)
    except Exception:
        pass  # Non-critical — don't crash if read receipt fails
