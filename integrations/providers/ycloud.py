# ycloud raw payload parser --> adapter
from integrations.providers.payload import (
    IncomingWhatsAppMessage,
    WhatsAppCustomer,
    WhatsAppBusiness,
    WhatsAppMedia,
    WhatsAppInteractive,
    WhatsAppButtonReply,
)


class WhatsappPayloadParserError(Exception):
    pass

def parser_ycloud(raw: dict) -> IncomingWhatsAppMessage:
    try:
        message = raw['whatsappInboundMessage']
        msg_type = message['type']

        customer = WhatsAppCustomer(
            phone = message['from'],
            provider_user_id = message['fromUserId']
        )

        business = WhatsAppBusiness(
            phone = message['to'],
        )

        # YCloud sends text as {"body": "..."}; older payloads may send a plain
        # string. Only present on text messages — absent on documents/media.
        text = None
        if msg_type == 'text':
            raw_text = message.get('text')
            text = raw_text.get('body') if isinstance(raw_text, dict) else raw_text

        media = None
        if msg_type == 'document':
            document = message['document']
            media = WhatsAppMedia(
                id=document.get("id"),
                mime_type=document.get('mime_type'),
                filename=document.get('filename'),
                link=document.get("link"),
            )

        # Interactive replies (button / list taps). YCloud mirrors Meta's message
        # object but has been observed to use either snake_case (button_reply) or
        # camelCase (buttonReply) — accept both so confirmation buttons resolve to
        # the reply title instead of an empty message.
        interactive = None
        if msg_type == 'interactive':
            inter = message.get('interactive') or {}
            reply = (
                inter.get('button_reply')
                or inter.get('buttonReply')
                or inter.get('list_reply')
                or inter.get('listReply')
                or {}
            )
            interactive = WhatsAppInteractive(
                type=inter.get('type'),
                button_reply=WhatsAppButtonReply(
                    id=reply.get('id'),
                    title=reply.get('title'),
                ),
            )

        payload = IncomingWhatsAppMessage(
            provider = 'ycloud',
            provider_message_id=None,
            whatsapp_message_id=message['id'],
            message_type=msg_type,
            text=text,
            customer = customer,
            business = business,
            media=media,
            interactive=interactive,
            raw = raw
        )

        return payload


    except Exception as e:
        raise WhatsappPayloadParserError("YCloud parser error") from e