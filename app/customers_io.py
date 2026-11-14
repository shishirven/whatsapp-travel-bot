"""Read a client CSV and create or update trusted customers.

Shared by the command-line importer and the staff-desk upload. Phone numbers
are normalised to WhatsApp digits before anything is written.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from io import StringIO

from app.db import Database
from app.phones import normalise_au_phone

_NAME = {"name", "customer", "client", "full_name"}
_PHONE = {"phone", "phones", "mobile", "mobiles", "wa_id", "whatsapp", "number", "numbers"}
_ADDRESS = {"address", "addresses", "street"}
_SUBURB = {"suburb", "suburbs"}
_NOTES = {"notes", "note", "comment", "comments"}


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def message(self) -> str:
        prefix = "Dry run. " if self.dry_run else ""
        lines = [
            f"{prefix}Created {self.created}, updated {self.updated}, skipped {self.skipped}."
        ]
        lines.extend(self.errors)
        return "\n".join(lines)


def import_csv_text(db: Database, text: str, *, dry_run: bool = False) -> ImportResult:
    rows = parse_client_csv(text)
    result = ImportResult(dry_run=dry_run)
    claimed: dict[str, int | str] = {}
    for index, row in enumerate(rows, start=2):
        if not row["name"] and not row["phones"] and not row["address"] and not row["notes"]:
            result.skipped += 1
            continue
        try:
            phones = _normalise_all(row["phones"])
            if not row["name"]:
                raise ValueError("Name is missing")
            if not phones:
                raise ValueError("No phone number")
            if dry_run:
                action, owner = _plan(db, phones, claimed)
                token: int | str = owner if action == "updated" else f"new-{index}"
                for phone in phones:
                    claimed[phone] = token
            else:
                action = db.import_client(
                    name=row["name"],
                    phones=phones,
                    address=row["address"],
                    notes=row["notes"],
                )
            if action == "created":
                result.created += 1
            else:
                result.updated += 1
        except ValueError as exc:
            result.errors.append(f"Row {index}: {exc}")
            result.skipped += 1
    return result


def parse_client_csv(text: str) -> list[dict]:
    sample = text.lstrip("\ufeff")
    if not sample.strip():
        raise ValueError("CSV is empty")
    reader = csv.DictReader(StringIO(sample))
    if not reader.fieldnames:
        raise ValueError("CSV needs a header row")
    fields = {_header(name): name for name in reader.fieldnames if name and name.strip()}
    name_col = _pick(fields, _NAME)
    phone_col = _pick(fields, _PHONE)
    if name_col is None or phone_col is None:
        raise ValueError("CSV needs name and phone columns")
    address_col = _pick(fields, _ADDRESS)
    suburb_col = _pick(fields, _SUBURB)
    notes_col = _pick(fields, _NOTES)
    rows = []
    for raw in reader:
        address_parts = []
        if address_col:
            address_parts.append((raw.get(address_col) or "").strip())
        if suburb_col:
            address_parts.append((raw.get(suburb_col) or "").strip())
        address = ", ".join(part for part in address_parts if part) or None
        rows.append(
            {
                "name": (raw.get(name_col) or "").strip(),
                "phones": _split_phones(raw.get(phone_col) or ""),
                "address": address,
                "notes": (raw.get(notes_col) or "").strip() or None if notes_col else None,
            }
        )
    return rows


def _plan(
    db: Database, phones: list[str], claimed: dict[str, int | str]
) -> tuple[str, int | str | None]:
    owners: set[int | str] = set()
    for phone in phones:
        if phone in claimed:
            owners.add(claimed[phone])
            continue
        existing = db.get_customer_by_wa(phone)
        if existing:
            owners.add(existing["id"])
    if len(owners) > 1:
        raise ValueError("Those phones already belong to different customers")
    if not owners:
        return "created", None
    return "updated", next(iter(owners))


def _normalise_all(raw_phones: list[str]) -> list[str]:
    phones: list[str] = []
    seen: set[str] = set()
    for raw in raw_phones:
        number = normalise_au_phone(raw)
        if number is None:
            raise ValueError(f"Could not read phone {raw!r}")
        if number not in seen:
            seen.add(number)
            phones.append(number)
    return phones


def _split_phones(value: str) -> list[str]:
    parts = re.split(r"[;|/]+|\n+|\s+and\s+", value)
    return [part.strip() for part in parts if part.strip()]


def _header(name: str) -> str:
    return re.sub(r"[\s\-]+", "_", name.strip().lower())


def _pick(fields: dict[str, str], options: set[str]) -> str | None:
    for option in options:
        if option in fields:
            return fields[option]
    return None
