# meta raw payload parser --> adapter

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

def parse_meta(raw: dict) -> IncomingWhatsAppMessage:
    try:
        entry = raw['entry'][0]
        changes = entry['changes'][0]
        value = changes['value']
        messages = value['messages']
        contacts = value['contacts']

        customer = WhatsAppCustomer(
            phone = messages[0]['from'],
            name = contacts[0]['profile']['name'],
            wa_id = contacts[0]['wa_id']
        )

        business = WhatsAppBusiness(
            phone = value["metadata"].get("display_phone_number"),
            phone_number_id = value["metadata"].get("phone_number_id"),
            waba_id = entry['id']
        )

        msg = messages[0]
        msg_type = msg['type']

        text = msg['text']['body'] if msg_type == 'text' else None

        media = None
        if msg_type == 'document':
            media = WhatsAppMedia(
                id=msg['document']["id"],
                mime_type=msg['document']['mime_type'],
                filename=msg['document']["filename"],
            )

        interactive = None
        if msg_type == 'interactive':
            inter = msg['interactive']
            button_reply = inter.get('button_reply', {})
            interactive = WhatsAppInteractive(
                type=inter.get('type'),
                button_reply=WhatsAppButtonReply(
                    id=button_reply.get('id'),
                    title=button_reply.get('title'),
                ),
            )

        payload = IncomingWhatsAppMessage(
            provider = 'meta',
            event_id = None,
            event_type = None,
            whatsapp_message_id = messages[0]['id'],
            customer = customer,
            business = business,
            provider_message_id = None,
            message_type = msg_type,
            text=text,
            media=media,
            interactive=interactive,
            raw = raw
        )

        return payload

    except Exception as e:
        raise WhatsappPayloadParserError("failed to parse meta webhook payload") from e