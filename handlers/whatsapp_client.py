"""Meta WhatsApp Cloud API client — send messages."""
import json
import logging

import requests
import config
from integrations.providers.payload import OutboundChannel, WhatsAppMedia

logger = logging.getLogger(__name__)

_GRAPH = "https://graph.facebook.com/v22.0"


def _as_interactive(text: str) -> dict | None:
    """Return the parsed interactive-button payload if `text` is one, else None.

    Interactive button messages are built by BaseButton.get_buttons as a
    ``json.dumps`` string and handed to send_text_message through the same
    `text` argument as plain messages. We recognise them here by shape
    (top-level ``type`` of button/list plus an ``action``) rather than by
    "is this parseable as JSON", so an ordinary reply that happens to look
    like JSON is never mistaken for buttons.
    """
    stripped = text.strip() if isinstance(text, str) else text
    if not (isinstance(stripped, str) and stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        obj = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    if isinstance(obj, dict) and obj.get("type") in ("button", "list") and "action" in obj:
        return obj
    return None


def prepend_text_to_buttons(prefix: str, reply: str) -> str:
    """Combine leading `prefix` text with a `reply` that may be a button payload.

    WhatsApp can't carry free text *and* interactive buttons as separate parts
    of one message — the buttons message already owns its body text. So when
    `reply` is a button payload we fold `prefix` into that body (keeping the
    JSON valid); otherwise we just join the two as plain text. This prevents
    the "raw JSON delivered as text" bug that happened when `prefix + reply`
    corrupted the button JSON.
    """
    interactive = _as_interactive(reply)
    if interactive is None:
        return f"{prefix}\n\n{reply}" if prefix else reply
    body = interactive.setdefault("body", {})
    existing = body.get("text", "")
    body["text"] = f"{prefix}\n\n{existing}" if prefix else existing
    return json.dumps(interactive)


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

    interactive = _as_interactive(text)

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
        if interactive is not None:
            payload["type"] = "interactive"
            payload["interactive"] = interactive
            payload.pop("text")

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

        if interactive is not None:
            payload["type"] = "interactive"
            payload["interactive"] = interactive
            payload.pop("text")

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
