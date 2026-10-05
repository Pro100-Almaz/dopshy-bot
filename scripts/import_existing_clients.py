"""Import pre-bot WhatsApp contacts so the academy bots stay silent for them.

Reads every cell of a CSV (or a plain one-number-per-line .txt) and keeps the
values that look like Kazakhstan phone numbers, so a raw WhatsApp Business /
CRM export works without picking a column. Safe to re-run: duplicates are skipped.

Usage:
    poetry run python scripts/import_existing_clients.py contacts.csv
    poetry run python scripts/import_existing_clients.py contacts.csv --source crm --dry-run
"""

import argparse
import csv
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.repo.existing_client_repo import (  # noqa: E402
    add_existing_clients,
    canonical_kz_phone,
)

logger = logging.getLogger(__name__)


def read_phones(path: str) -> list[str]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        cells = [cell for row in csv.reader(fh, dialect) for cell in row]
    phones = []
    for cell in cells:
        key = canonical_kz_phone(cell)
        if len(key) == 11 and key.startswith("7"):
            phones.append(key)
    return list(dict.fromkeys(phones))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", help="CSV/TXT file with phone numbers")
    parser.add_argument("--source", default="whatsapp_import", help="label stored with each row")
    parser.add_argument("--dry-run", action="store_true", help="only print what would be imported")
    args = parser.parse_args()

    phones = read_phones(args.path)
    print(f"Found {len(phones)} unique phone numbers in {args.path}")
    if args.dry_run:
        for phone in phones[:20]:
            print("  ", phone)
        if len(phones) > 20:
            print(f"   … and {len(phones) - 20} more")
        return
    added = add_existing_clients(phones, source=args.source)
    print(f"Imported {added} new, {len(phones) - added} already present")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
