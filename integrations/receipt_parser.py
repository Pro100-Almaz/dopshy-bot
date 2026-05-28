"""Parse Kaspi / Halyk payment-receipt PDFs into structured fields.

Both banks produce text-based PDFs (no OCR needed). Field labels and values are
sometimes on separate lines, so patterns are matched position-independently.
"""

import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

# normalize the various unicode spaces banks use inside "20 000"
_SPACES = "    "

_AMOUNT_RE = re.compile(r"([\d" + _SPACES + r"]+)\s*₸")
_DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})")
_BIN_RE = re.compile(r"ИИН/БИН\s*продавца[\s\n]*(\d{12})")
_KASPI_REF_RE = re.compile(r"№\s*чека[\s\n]*([A-Za-z]{0,4}\d{6,})")
_HALYK_REF_RE = re.compile(r"№\s*квитанции[\s\n]*(\d{6,})")
_RECIPIENT_RE = re.compile(r"Получатель[\s\n]*([^\n]+)")
_PHONE_RE = re.compile(r"Куда[\s\n]*([+\d\s()\-]{8,})")


def extract_text(pdf_bytes: bytes) -> str:
    from io import BytesIO

    from pypdf import PdfReader

    reader = PdfReader(BytesIO(pdf_bytes))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def _parse_amounts(text: str) -> int | None:
    amounts = []
    for m in _AMOUNT_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(1))
        if digits:
            amounts.append(int(digits))
    return max(amounts) if amounts else None


def _parse_date(text: str) -> datetime | None:
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%d.%m.%Y %H:%M")
    except ValueError:
        return None


def _detect_bank(text: str) -> str:
    low = text.lower()
    if "квитанции" in low or "перевод" in low:
        return "halyk"
    if "фискальный" in low or "kaspi" in low:
        return "kaspi"
    return "unknown"


def parse_receipt(pdf_bytes: bytes) -> dict:
    """Return {bank, amount, bin, name, phone, date, ref, raw_text}. Fields are None if absent."""
    try:
        text = extract_text(pdf_bytes)
    except Exception as exc:
        logger.error("Receipt PDF text extraction failed: %s", exc)
        return {"bank": "unknown", "amount": None, "bin": None, "name": None,
                "phone": None, "date": None, "ref": None, "raw_text": ""}

    bank = _detect_bank(text)
    bin_m = _BIN_RE.search(text)
    name_m = _RECIPIENT_RE.search(text)
    phone_m = _PHONE_RE.search(text)
    ref = None
    if bank == "kaspi":
        rm = _KASPI_REF_RE.search(text)
        ref = rm.group(1) if rm else None
    elif bank == "halyk":
        rm = _HALYK_REF_RE.search(text)
        ref = rm.group(1) if rm else None

    phone = re.sub(r"\D", "", phone_m.group(1)) if phone_m else None

    return {
        "bank": bank,
        "amount": _parse_amounts(text),
        "bin": bin_m.group(1) if bin_m else None,
        "name": name_m.group(1).strip() if name_m else None,
        "phone": phone or None,
        "date": _parse_date(text),
        "ref": ref,
        "raw_text": text,
    }
