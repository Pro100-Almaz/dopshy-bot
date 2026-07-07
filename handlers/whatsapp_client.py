"""Meta WhatsApp Cloud API client — send messages."""
import json
import logging

import requests
import config
from integrations.providers.payload import OutboundChannel, WhatsAppMedia

logger = logging.getLogger(__name__)

_GRAPH = "https://graph.facebook.com/v22.0"


def download_media(channel: OutboundChannel, media:WhatsAppMedia) -> bytes | None:
    """Resolve a media_id to its temporary URL and download the bytes. None on failure."""
    bot_config = config.get_bot_config(channel.phone_number_id)
    if not bot_config or not media.id:
        return None
    if channel.provider == "meta":
        headers = {"Authorization": f"Bearer {bot_config['access_token']}"}
        try:
            meta = requests.get(f"{_GRAPH}/{media.id}", headers=headers, timeout=10)
            meta.raise_for_status()
            url = meta.json().get("url")
            if not url:
                return None
            media = requests.get(url, headers=headers, timeout=30)
            media.raise_for_status()
            return media.content
        except Exception as exc:
            logger.error("Media download failed for %s: %s", media.id, exc)
            return None
    else:
        try:
            response = requests.get(
                media.link,
                headers={"X-API-Key": bot_config['ycloud_api_key']},
                timeout=30
            )
            response.raise_for_status()
            return response.content
        except Exception as exc:
            logger.error("Media download failed for %s: %s", media.id, exc)
            return None


def send_text_message(channel: OutboundChannel, to: str, text: str) -> dict:
    """
    Send a plain-text WhatsApp message.

    Args:
        to: Recipient's phone number in international format (e.g. "77001234567")
           or a group JID.
        text: Message body.
    Returns:
        API response JSON.
    """
    provider = channel.provider
    bot_config = config.get_bot_config(channel.phone_number_id)
    if not bot_config:
        raise ValueError(f"Bot config not found for phone number ID: {channel.phone_number_id}")

    if provider == 'meta':
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
        try:
            payload["interactive"] = json.loads(text)
            payload["type"] = "interactive"
            payload.pop("text")
        except Exception:
            pass

        url = config.get_whatsapp_api_url(channel.phone_number_id)

    else:
        payload = {
            "from": bot_config["ycloud_from"],
            "to": to,
            "type": "text",
            "text": {"body": text},
        }

        headers = {
            "X-API-Key": bot_config['ycloud_api_key'],
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        url = config.get_ycloud_api_url()

    response = requests.post(
        url,
        json=payload,
        headers=headers,
        timeout=10
    )
    response.raise_for_status()
    return response.json()


def mark_as_read(channel: OutboundChannel, message_id: str) -> None:
    provider = channel.provider

    bot_config = config.get_bot_config(channel.phone_number_id)
    if not bot_config:
        raise ValueError(f"Unknown phone_number_id: {channel.phone_number_id}")


    if provider == 'meta':
        headers = {
            "Authorization": f"Bearer {bot_config['access_token']}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        url = config.get_whatsapp_api_url(channel.phone_number_id)

    else:
        headers = {
            "accept": "application/json",
            "X-API-KEY": bot_config['ycloud_api_key']
        }

        url = config.get_ycloud_mark_as_read_url(message_id)

    try:
        requests.post(
            url,
            headers=headers,
            timeout=5,
        )

    except Exception:
        pass  # Non-critical — don't crash if read receipt fails
