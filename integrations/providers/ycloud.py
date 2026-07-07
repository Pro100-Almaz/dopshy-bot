# ycloud raw payload parser --> adapter
from integrations.providers.payload import IncomingWhatsAppMessage, WhatsAppCustomer, WhatsAppBusiness, WhatsAppMedia


class WhatsappPayloadParserError(Exception):
    pass

def parser_ycloud(raw: dict) -> IncomingWhatsAppMessage:
    try:
        message = raw['whatsappInboundMessage']

        customer = WhatsAppCustomer(
            phone = message['from'],
            provider_user_id = message['fromUserId']
        )

        business = WhatsAppBusiness(
            phone = message['to'],
        )

        payload = IncomingWhatsAppMessage(
            provider = 'ycloud',
            provider_message_id=None,
            whatsapp_message_id=message['id'],
            message_type=message['type'],
            text=message['text'],
            customer = customer,
            business = business,
            raw = raw
        )
        if 'document' in message:
            media = WhatsAppMedia(
                id=message['document']["id"],
                mime_type=message['document']['mime_type'],
                link=message['document']["link"]
            )
            payload.media = media

        return payload


    except Exception as e:
        raise WhatsappPayloadParserError("YCloud parser error") from e