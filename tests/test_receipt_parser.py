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
