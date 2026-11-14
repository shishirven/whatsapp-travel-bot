"""Import existing clients from a CSV file.

    python -m app.import_clients clients.csv
    python -m app.import_clients clients.csv --dry-run
    python -m app.import_clients clients.csv --db data/tradie.db

Columns: name, phone (or phones), address, notes. Several numbers can sit in
the phone cell, separated by semicolons. Australian numbers are stored as
WhatsApp digits (`0412 345 678` becomes `61412345678`).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.customers_io import import_csv_text
from app.db import Database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import existing clients into the tradie assistant."
    )
    parser.add_argument("csv_path", help="CSV with name, phone, address, and notes")
    parser.add_argument(
        "--db",
        dest="db_path",
        default=None,
        help="SQLite file. Defaults to SQLITE_PATH or data/tradie.db",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check the file and print what would change, without writing",
    )
    args = parser.parse_args(argv)
    path = Path(args.csv_path)
    if not path.is_file():
        print(f"No such file: {path}", file=sys.stderr)
        return 1
    db_path = args.db_path or _default_db_path()
    db = Database(db_path)
    text = path.read_text(encoding="utf-8-sig")
    try:
        result = import_csv_text(db, text, dry_run=args.dry_run)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(result.message)
    return 1 if result.errors else 0


def _default_db_path() -> str:
    from app.config import Settings

    return Settings.from_env().sqlite_path


if __name__ == "__main__":
    raise SystemExit(main())
