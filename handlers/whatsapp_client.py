"""Meta WhatsApp Cloud API client — send messages."""

import requests
import config


def send_text_message(phone_number_id: str, to: str, text: str) -> dict:
    """
    Send a plain-text WhatsApp message.

    Args:
        to: Recipient's phone number in international format (e.g. "77001234567")
           or a group JID.
        text: Message body.
    Returns:
        API response JSON.
    """
    bot_config = config.get_bot_config(phone_number_id)
    if not bot_config:
        raise ValueError(f"Bot config not found for phone number ID: {phone_number_id}")

    headers = {
        "Authorization": f"Bearer {bot_config['access_token']}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    response = requests.post(
        config.get_whatsapp_api_url(phone_number_id),
        json=payload,
        headers=headers,
        timeout=10
    )
    response.raise_for_status()
    return response.json()


def mark_as_read(phone_number_id: str, message_id: str) -> None:
    """Mark an incoming message as read (shows blue ticks)."""
    bot_config = config.get_bot_config(phone_number_id)
    if not bot_config:
        raise ValueError(f"Unknown phone_number_id: {phone_number_id}")

    headers = {
        "Authorization": f"Bearer {bot_config['access_token']}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    try:
        requests.post(
            config.get_whatsapp_api_url(phone_number_id),
            json=payload,
            headers=headers,
            timeout=5,
        )
    except Exception:
        pass  # Non-critical — don't crash if read receipt fails
