from fastapi import FastAPI
from pydantic import BaseModel
from typing import Union, List, Dict, Any

# Import your two AI functions
from router import route_incoming_message
from extractor import extract_booking_details

# Initialize the web API application
app = FastAPI()

# Intents that map to static FAQ answers handled by the backend team.
FAQ_TYPES = ["question_price", "question_slots", "question_field_size"]

# Define the expected structure of the incoming request body
class WebhookPayload(BaseModel):
    ChatHistory: Union[str, List[Dict[str, Any]]] = ""

@app.post("/api/whatsapp-handler")
def handle_whatsapp_message(payload: WebhookPayload):
    chat_history = payload.ChatHistory

    # Step 1: classify the latest message in the context of the full session log.
    message_type = route_incoming_message(chat_history)

    # Step 2: branch on intent.
    if message_type in FAQ_TYPES:
        return {"action": "send_faq", "type": message_type}

    if message_type in ["booking_new", "booking_continue"]:
        extracted_data = extract_booking_details(chat_history)

        # Any field still None (null) is information we still need to ask the user for.
        missing_fields = [
            key for key, value in extracted_data.items() if value is None
        ]

        return {
            "action": "process_booking",
            "data": extracted_data,
            "missingFields": missing_fields
        }

    # Fallback for greetings / unrelated input.
    return {"action": "fallback", "type": message_type}