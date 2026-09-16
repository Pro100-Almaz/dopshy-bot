"""Parser tests against the real sample receipts in receipts/."""

import os

import pytest

from integrations.receipt_parser import parse_receipt

_RECEIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "receipts")


def _parse(name):
    with open(os.path.join(_RECEIPTS, name), "rb") as fh:
        return parse_receipt(fh.read())


# (file, bank, amount, bin, phone, ref)
_CASES = [
    ("receipt.pdf",                   "kaspi", 20000, "870203301478", None,          "QR15586394175"),
    ("receipt (1).pdf",               "kaspi", 10000, "870203301478", None,          "QR15589757949"),
    ("receipt (3).pdf",               "kaspi",  2000, "250740003149", None,          "QR15588281978"),
    ("download.pdf",                  "kaspi",  5000, "870203301478", None,          "QR15159900578"),
    ("halyk_receipt_3125347834.pdf",  "halyk", 25000, None,           "77029721819", "3125347834"),
    ("halyk_receipt_3250927742.pdf",  "halyk", 20000, None,           "77029721819", "3250927742"),
]


@pytest.mark.parametrize("name,bank,amount,bin_,phone,ref", _CASES)
def test_parse(name, bank, amount, bin_, phone, ref):
    d = _parse(name)
    assert d["bank"] == bank
    assert d["amount"] == amount
    assert d["bin"] == bin_
    assert d["phone"] == phone
    assert d["ref"] == ref
    assert d["date"] is not None


def test_halyk_recipient_name():
    assert "Мухтар" in _parse("halyk_receipt_3125347834.pdf")["name"]


# --- Kazakh-language receipts -------------------------------------------------
# No committed Kazakh sample PDFs, so exercise the full parse_receipt() path by
# feeding representative extracted text (labels the Kaspi/Halyk KZ UIs emit).

def _parse_text(monkeypatch, text):
    monkeypatch.setattr("integrations.receipt_parser.extract_text", lambda _b: text)
    return parse_receipt(b"pdf")


def test_kazakh_kaspi(monkeypatch):
    text = (
        "Фискалдық түбіртек\n"
        "Kaspi.kz\n"
        "Түбіртек № QR15586394175\n"
        "Сатушының ЖСН/БСН 870203301478\n"
        "Алушы DOPSHY\n"
        "20 000 ₸\n"
        "26.07.2026 12:30\n"
    )
    d = _parse_text(monkeypatch, text)
    assert d["bank"] == "kaspi"
    assert d["amount"] == 20000
    assert d["bin"] == "870203301478"
    assert d["ref"] == "QR15586394175"
    assert d["name"] and "DOPSHY" in d["name"]
    assert d["date"] is not None


def test_kazakh_halyk(monkeypatch):
    text = (
        "Ақша аудару\n"
        "Түбіртек № 3125347834\n"
        "Алушы Мухтар А.\n"
        "Қайда +7 702 972 1819\n"
        "25 000 ₸\n"
        "26.07.2026 12:30\n"
    )
    d = _parse_text(monkeypatch, text)
    assert d["bank"] == "halyk"
    assert d["amount"] == 25000
    assert d["ref"] == "3125347834"
    assert d["phone"] == "77029721819"
    assert "Мухтар" in d["name"]
    assert d["date"] is not None
