#Universal Payload Class --> Port
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Provider = Literal['meta', 'ycloud']

@dataclass
class WhatsAppCustomer:
    phone: str | None
    name: str | None = None

    #Meta --> contaxts[0].wa_id
    #Ycloud --> fromUserId, fromParentUserId
    wa_id: str | None = None
    provider_user_id : str | None = None
    provider_parent_user_id: str | None = None


@dataclass
class WhatsAppBusiness:
    #normal phone number
    phone: str | None = None
    #meta only field
    phone_number_id : str | None = None
    waba_id : str | None = None


@dataclass
class WhatsAppMedia:
    id: str | None = None
    link: str | None = None
    mime_type: str | None = None
    filename: str | None = None


@dataclass
class WhatsAppButtonReply:
    id: str | None = None
    title: str | None = None
    mime_type: str | None = None


@dataclass
class WhatsAppInteractive:
    type: str | None = None  # 'button_reply' | 'list_reply'
    button_reply: WhatsAppButtonReply | None = None


@dataclass
class IncomingWhatsAppMessage:
    provider: Provider

    provider_message_id : str | None
    whatsapp_message_id: str | None

    message_type: str
    text: str | None

    customer: WhatsAppCustomer
    business: WhatsAppBusiness

    event_id: str | None = None
    event_type: str | None = None

    sent_at: datetime | None = None

    context_message_id: str | None = None

    media: WhatsAppMedia | None = None
    interactive: WhatsAppInteractive | None = None

    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class OutboundChannel:
    provider: Provider
    phone_number_id: str


