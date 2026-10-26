"""Client for the platform backend's booking-prefill API.

The bot collects booking details in chat; this turns them into a short-lived
link to the website booking form (POST {PLATFORM_API_URL}/bot/prefill). The
link is a convenience only: every failure returns None and the chat flow goes
on, so nothing here may raise into a conversation turn.
"""

import json
import logging
import re

import requests

import config
from integrations.repo.utils import normalize_phone

logger = logging.getLogger(__name__)

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def is_enabled() -> bool:
    return bool(config.PLATFORM_HANDOFF_ENABLED and config.PLATFORM_API_URL and config.BOT_PREFILL_API_KEY)


def build_payload(phone: str, *, customer_name: str | None = None, field: int | None = None,
                  date: str | None = None, time_start: str | None = None,
                  time_end: str | None = None, lang: str | None = None,
                  conversation_id: str | None = None) -> dict:
    """Only known, valid fields are sent (the backend rejects unknown keys and bad values).

    The backend wants HH:MM with start < end, both or neither. Ranges that cross
    midnight or end at "24:00" cannot be expressed, so the times are dropped and
    the user picks them on the site.
    """
    payload: dict = {"phone": normalize_phone(phone)}
    if customer_name and customer_name.strip():
        payload["customer_name"] = customer_name.strip()[:128]
    if field:
        payload["field"] = int(field)
    if date:
        payload["date"] = date
    if (time_start and time_end and _HHMM.match(time_start) and _HHMM.match(time_end)
            and time_start < time_end and date):
        payload["time_start"] = time_start
        payload["time_end"] = time_end
    if lang in ("ru", "kk"):
        payload["lang"] = lang
    if conversation_id:
        payload["conversation_id"] = conversation_id[:128]
    return payload


def create_prefill_link(phone: str, **fields) -> str | None:
    """Return the landing URL for this user's data, or None if unavailable."""
    if not is_enabled():
        return None
    payload = build_payload(phone, **fields)
    if not payload.get("phone"):
        return None
    url = f"{config.PLATFORM_API_URL}/bot/prefill"
    headers = {"X-API-Key": config.BOT_PREFILL_API_KEY, "Content-Type": "application/json"}
    for attempt in (1, 2):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=config.PLATFORM_PREFILL_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("[PREFILL] attempt %d: backend unreachable: %s", attempt, exc)
            continue
        if resp.status_code >= 500:
            logger.warning("[PREFILL] attempt %d: backend %d", attempt, resp.status_code)
            continue
        if resp.status_code != 201:
            # 4xx will not improve on retry (bad key, rejected payload).
            logger.warning("[PREFILL] rejected: %d %.200s", resp.status_code, resp.text)
            return None
        try:
            link = resp.json()["data"]["url"]
        except (ValueError, KeyError, TypeError):
            logger.warning("[PREFILL] unexpected response shape")
            return None
        return link if isinstance(link, str) and link.startswith("http") else None
    return None


# The offer sits in front of the bot's normal question, so it ends with "continue here:".
# Variant depends on what the user has already said: nothing / asked about free times /
# gave a date / gave date and time.
_NOTES = {
    "ru": {
        "start": "Спасибо за обращение! 🙌\nДля создания брони удобнее перейти по ссылке — всё выбирается в пару касаний, время вводить не нужно:\n{url}\n\nЕсли хотите продолжить в этом чате:",
        "availability": "Спасибо за обращение! 🙌\nВсе свободные слоты сразу видны на сайте — время выбирается одним касанием:\n{url}\n\nЕсли хотите продолжить в этом чате:",
        "date": "Спасибо! 🙌\nДату мы уже подставили. Время удобнее выбрать на сайте одним касанием:\n{url}\n\nЕсли хотите продолжить в этом чате:",
        "slot": "Спасибо! 🙌\nДату, время и ваши данные мы уже подставили — осталось нажать «Забронировать»:\n{url}\n\nЕсли хотите продолжить в этом чате:",
    },
    "kk": {
        "start": "Хабарласқаныңызға рахмет! 🙌\nБрондау үшін сілтемеге өту ыңғайлырақ — барлығы бірнеше басумен таңдалады, уақытты теру қажет емес:\n{url}\n\nЕгер осы чатта жалғастырғыңыз келсе:",
        "availability": "Хабарласқаныңызға рахмет! 🙌\nБарлық бос уақыт сайтта бірден көрінеді — уақытты бір басумен таңдайсыз:\n{url}\n\nЕгер осы чатта жалғастырғыңыз келсе:",
        "date": "Рахмет! 🙌\nКүнді толтырып қойдық. Уақытты сайтта бір басумен таңдаған ыңғайлырақ:\n{url}\n\nЕгер осы чатта жалғастырғыңыз келсе:",
        "slot": "Рахмет! 🙌\nКүнді, уақытты және деректеріңізді толтырып қойдық — «Брондау» түймесін басу ғана қалды:\n{url}\n\nЕгер осы чатта жалғастырғыңыз келсе:",
    },
}
# WhatsApp interactive reply-button bodies are capped at 1024 characters.
_BUTTON_BODY_LIMIT = 1024

_AVAILABILITY_RE = re.compile(
    r"свобод|доступн|какое время|какие.*(время|слот|час)|есть (время|места|слоты)|бос\b|бос уақыт|қашан",
    re.IGNORECASE,
)


def asks_availability(text: str | None) -> bool:
    return bool(text and _AVAILABILITY_RE.search(text))


def note(lang: str, url: str, *, has_date: bool = False, has_time: bool = False,
         availability: bool = False) -> str:
    """The offer text for the user's current stage (see _NOTES)."""
    notes = _NOTES.get(lang, _NOTES["ru"])
    if has_date and has_time:
        key = "slot"
    elif has_date:
        key = "date"
    elif availability:
        key = "availability"
    else:
        key = "start"
    return notes[key].format(url=url)


def prepend_note(reply: str, text: str) -> str:
    """Put `text` in front of a bot reply without breaking its format.

    Interactive replies are JSON (see BaseButton.get_buttons) that whatsapp_client
    parses with json.loads, so the note goes into the body text instead of around
    the payload. If it does not fit, the reply is returned unchanged.
    """
    if not reply.lstrip().startswith("{"):
        return text + "\n\n" + reply
    try:
        payload = json.loads(reply)
        body = text + "\n\n" + payload["body"]["text"]
    except (ValueError, KeyError, TypeError):
        return reply
    if len(body) > _BUTTON_BODY_LIMIT:
        return reply
    payload["body"]["text"] = body
    return json.dumps(payload)


_redis_client = None


def claim_offer(chat_id: str) -> bool:
    """True exactly once per chat per 24h (Redis SET NX), across gunicorn workers.

    Fails closed: if Redis is unreachable the link is simply not offered, so an
    outage can never turn into a link on every message.
    """
    global _redis_client
    try:
        if _redis_client is None:
            import redis
            _redis_client = redis.Redis.from_url(config.REDIS_URL, socket_timeout=1, socket_connect_timeout=1)
        return bool(_redis_client.set(f"platform_handoff:{chat_id}", "1", nx=True, ex=86400))
    except Exception as exc:  # noqa: BLE001 - never break a chat turn
        logger.warning("[PREFILL] cannot record offer, skipping link: %s", exc)
        return False


def retry_later(chat_id: str, seconds: int = 120) -> None:
    """The link could not be created: shorten the once-per-chat block so a later message
    retries, but a down backend is not hit (2 attempts x timeout) on every message."""
    try:
        if _redis_client is not None:
            _redis_client.expire(f"platform_handoff:{chat_id}", seconds)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[PREFILL] cannot shorten offer block: %s", exc)


def release_offer(chat_id: str) -> None:
    """Forget that a link was offered (conversation reset / booking finished), so the next
    booking in this chat gets a fresh offer."""
    try:
        if _redis_client is not None:
            _redis_client.delete(f"platform_handoff:{chat_id}")
        elif config.PLATFORM_HANDOFF_ENABLED:
            import redis
            redis.Redis.from_url(config.REDIS_URL, socket_timeout=1, socket_connect_timeout=1).delete(
                f"platform_handoff:{chat_id}")
    except Exception as exc:  # noqa: BLE001 - never break a reset
        logger.warning("[PREFILL] cannot clear offer mark: %s", exc)
