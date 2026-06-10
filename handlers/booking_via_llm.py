from pydantic import BaseModel, Field
from typing import Optional, Literal
from openai import OpenAI
import datetime
import config
from integrations.repo.academy_repo import confirm_trial

client = OpenAI(api_key=config.OPENAI_API_KEY)

class Extracted_data(BaseModel):
    date: Optional[str] = Field(
        description="The date of the booking in DD-MM-YYYY format."
    )
    time_start: Optional[str] = Field(
        description="The start time of the booking in HH:MM format."
    )
    duration: Optional[str] = Field(
        description="The duration of the booking in hours."
    )
    field_type: Optional[str] = Field(
        description="The type of the field."
    )

class UserIntent(BaseModel):
    intent: Literal["question", "booking"] = Field(
        description="Categorize the user's message. Use 'booking' if they show any intent to rent a field, even if details are missing."
    )
    extracted_data: Extracted_data


def get_user_intent(user_message: str) -> str:
    today = datetime.date.today()

    system_prompt = f"""
    You are the natural language router for 'Допшы', a WhatsApp football field rental assistant.
    Today's date is {today}.

    Your job is to analyze the user's message, determine their intent, and extract any rental details they provided.
    - If they ask a general question, set intent to 'question'.
    - If they want to rent a field, set intent to 'booking' and extract the variables.
    - If a variable is not mentioned, return null for that field. Convert relative dates like 'tomorrow' to YYYY-MM-DD.
    """

    response = client.beta.chat.completions.parse(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        response_format = UserIntent,
    )

    return response.choices[0].message.content


# def save_intent_to_json(parsed_intent: UserIntent, filename="current_booking.json"):
#     # .model_dump_json() is built into Pydantic. It creates a perfect JSON string.
#     json_string = parsed_intent.model_dump_json(indent=4)
#
#     with open(filename, "w", encoding="utf-8") as file:
#         file.write(json_string)
#
#     print(f"Saved to {filename}")

# def handle_message(user_message: str):
#     # 1. Get the parsed object from the LLM
#     parsed_intent = get_user_intent(user_message)
#
#     # 2. Save it straight to the JSON file
#     save_intent_to_json(parsed_intent)
#
#     # 3. Route the logic using the object attributes
#     if parsed_intent.intent == "question":
#         handle_faq(user_message)
#
#     elif parsed_intent.intent == "booking":
#         data = parsed_intent.extracted_data
#
#         # Check if all required fields are present for a fast booking
#         if data.date and data.time_start and data.duration and data.field_type:
#             print("Fast Renting is detected. Sending straight to booking confirmation")
#             confirm_booking(data)
#
#         else:
#             print("Partial Booking detected. Sending to start_session().")
#             start_session(
#                 date=data.date,
#                 time_start=data.time_start,
#                 duration=data.duration,
#                 field_type=data.field_type
#             )
