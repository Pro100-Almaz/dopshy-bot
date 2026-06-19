from handlers.base_classes.base_asker import BaseAsker
from handlers.base_classes.base_format import BaseFormat
from handlers.llm_booking_flow import T, LlmBookingFlowHandler
from integrations import booking as booking_logic

def check_slots(data: dict, chat_id, user_text, sender_id, lang) -> str:
    asker = BaseAsker(T)
    formatter = BaseFormat(asker)
    """Rule 1: show all free fields and their time ranges for the given date."""
    lang = data.get("lang", "ru")
    date_str = data["date"]
    free = booking_logic.get_free_windows()
    day_windows = [w for w in free if str(w["date"]) == date_str]

    if not day_windows:
        return (
                asker.localize(lang, "no_slots_date", date=formatter.fmt_date(date_str, lang))
                + "\n\n" + formatter.format_available_dates(free, lang)
        )

    handler = LlmBookingFlowHandler()
    handler.handle(data, chat_id, user_text, sender_id, lang)

    windows_text = formatter.format_windows_by_field(day_windows)
    return (
            asker.localize(lang, "slots_header", date=formatter.fmt_date(date_str, lang))
            + "\n\n" + windows_text
            + "\n\n" + asker.localize(lang, "write_time")
    )