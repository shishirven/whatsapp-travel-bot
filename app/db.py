"""SQLite storage for customers, their WhatsApp numbers, and jobs.

A customer can have several numbers and several jobs. One conversation thread
is kept per number. Messages belong to a job so the desk can move them.

The connection is opened per call so FastAPI's threadpool does not share a
sqlite3 connection across threads. Opening the database migrates an older
file that still has one lead per conversation: each conversation becomes a
customer, a number, and one job. Lead ids are kept as job ids.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.phones import normalise_au_phone
from app.routing import JOB_STATUSES, OPEN_JOB_STATUSES

logger = logging.getLogger("tradie.db")

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    name_confirmed INTEGER NOT NULL DEFAULT 0,
    trusted INTEGER NOT NULL DEFAULT 0,
    addresses_json TEXT NOT NULL DEFAULT '[]',
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customer_numbers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    wa_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_id TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    active_job_id INTEGER,
    profile_name TEXT,
    source TEXT NOT NULL,
    ad_id TEXT,
    campaign TEXT,
    ad_headline TEXT,
    ad_source TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'quoted', 'booked', 'completed', 'cancelled')),
    source TEXT NOT NULL DEFAULT 'organic'
        CHECK (source IN ('ad', 'organic', 'website', 'qr', 'repeat')),
    possible_customer_id INTEGER REFERENCES customers(id),
    prior_client_asked INTEGER NOT NULL DEFAULT 0,
    prior_client_answer TEXT,
    address_prompted INTEGER NOT NULL DEFAULT 0,
    address TEXT,
    job_type TEXT,
    suburb TEXT,
    urgency TEXT,
    description TEXT,
    preferred_time TEXT,
    contact_name TEXT,
    score TEXT NOT NULL,
    score_reason TEXT NOT NULL,
    handoff INTEGER NOT NULL DEFAULT 0,
    handoff_reason TEXT,
    photos_json TEXT NOT NULL DEFAULT '[]',
    voice_json TEXT NOT NULL DEFAULT '[]',
    quote_json TEXT NOT NULL DEFAULT '{}',
    booking_json TEXT NOT NULL DEFAULT '{}',
    extracted_json TEXT NOT NULL DEFAULT '{}',
    ad_id TEXT,
    campaign TEXT,
    ad_headline TEXT,
    ad_source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_customer ON jobs(customer_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_jobs_conversation ON jobs(conversation_id, status);
CREATE INDEX IF NOT EXISTS idx_customer_numbers_customer ON customer_numbers(customer_id);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    job_id INTEGER REFERENCES jobs(id),
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    body TEXT NOT NULL,
    provider_message_id TEXT,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_provider
    ON messages(provider_message_id)
    WHERE provider_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, id);
"""

_LEGACY_LEAD_COLUMNS = {
    "job_type": "TEXT",
    "suburb": "TEXT",
    "urgency": "TEXT",
    "description": "TEXT",
    "preferred_time": "TEXT",
    "contact_name": "TEXT",
    "photos_json": "TEXT NOT NULL DEFAULT '[]'",
    "voice_json": "TEXT NOT NULL DEFAULT '[]'",
    "quote_json": "TEXT NOT NULL DEFAULT '{}'",
    "booking_json": "TEXT NOT NULL DEFAULT '{}'",
}

_JOB_SELECT = """
SELECT
    j.id, j.conversation_id, j.customer_id,
    j.status AS job_status, j.source AS job_source,
    j.possible_customer_id, j.prior_client_asked, j.prior_client_answer,
    j.address_prompted, j.address,
    j.job_type, j.suburb, j.urgency, j.description, j.preferred_time, j.contact_name,
    j.score, j.score_reason, j.handoff, j.handoff_reason,
    j.photos_json, j.voice_json, j.quote_json, j.booking_json, j.extracted_json,
    j.ad_id, j.campaign, j.ad_headline, j.ad_source,
    j.created_at, j.updated_at,
    c.wa_id, c.profile_name, c.source AS conversation_source,
    c.status AS conversation_status, c.active_job_id,
    cu.name AS customer_name, cu.addresses_json AS customer_addresses,
    cu.notes AS customer_notes, cu.trusted AS customer_trusted,
    cu.name_confirmed AS customer_name_confirmed,
    pc.name AS possible_customer_name,
    (
        SELECT body FROM messages m
        WHERE m.job_id = j.id
        ORDER BY m.id DESC
        LIMIT 1
    ) AS last_message
FROM jobs j
JOIN conversations c ON c.id = j.conversation_id
JOIN customers cu ON cu.id = j.customer_id
LEFT JOIN customers pc ON pc.id = j.possible_customer_id
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class Database:
    def __init__(self, path: str, clock=None) -> None:
        self.path = path
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(_SCHEMA)
            _migrate(conn)

    def timestamp(self) -> str:
        current = self.clock()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 3000")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def create_conversation(
        self,
        *,
        wa_id: str,
        profile_name: str | None,
        source: str,
        ad: dict | None = None,
        job_source: str = "organic",
        customer_id: int | None = None,
        prefill: dict | None = None,
        address: str | None = None,
    ) -> dict:
        now = self.timestamp()
        ad = ad or {}
        prefill = prefill or {}
        with self.connect() as conn:
            if customer_id is None:
                cursor = conn.execute(
                    """
                    INSERT INTO customers (
                        name, name_confirmed, trusted, addresses_json, notes,
                        created_at, updated_at
                    ) VALUES (?, 0, 0, '[]', NULL, ?, ?)
                    """,
                    (profile_name, now, now),
                )
                customer_id = cursor.lastrowid
            _ensure_number(conn, customer_id, wa_id, now)
            cursor = conn.execute(
                """
                INSERT INTO conversations (
                    wa_id, customer_id, profile_name, source, ad_id, campaign,
                    ad_headline, ad_source, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (
                    wa_id,
                    customer_id,
                    profile_name,
                    source,
                    ad.get("ad_id"),
                    ad.get("campaign"),
                    ad.get("headline"),
                    ad.get("source"),
                    now,
                    now,
                ),
            )
            conversation_id = cursor.lastrowid
            job_id = _insert_job(
                conn,
                customer_id=customer_id,
                conversation_id=conversation_id,
                source=job_source,
                ad=ad,
                contact_name=prefill.get("contact_name"),
                suburb=prefill.get("suburb"),
                address=address,
                now=now,
            )
            conn.execute(
                "UPDATE conversations SET active_job_id = ? WHERE id = ?",
                (job_id, conversation_id),
            )
            return _conversation(conn, conversation_id)

    def create_job(
        self,
        *,
        customer_id: int,
        conversation_id: int,
        source: str,
        ad: dict | None = None,
        contact_name: str | None = None,
        suburb: str | None = None,
        address: str | None = None,
    ) -> dict:
        now = self.timestamp()
        with self.connect() as conn:
            job_id = _insert_job(
                conn,
                customer_id=customer_id,
                conversation_id=conversation_id,
                source=source,
                ad=ad or {},
                contact_name=contact_name,
                suburb=suburb,
                address=address,
                now=now,
            )
            conn.execute(
                """
                UPDATE conversations
                SET active_job_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (job_id, now, conversation_id),
            )
            return _job(conn, job_id)

    def get_conversation(self, conversation_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_conversation_by_wa(self, wa_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE wa_id = ?", (wa_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_or_create_conversation(
        self,
        *,
        wa_id: str,
        profile_name: str | None,
        source: str,
        job_source: str = "organic",
        ad: dict | None = None,
    ) -> dict:
        existing = self.get_conversation_by_wa(wa_id)
        if existing:
            if profile_name and not existing["profile_name"]:
                return self.set_profile_name(existing["id"], profile_name)
            return existing
        return self.create_conversation(
            wa_id=wa_id,
            profile_name=profile_name,
            source=source,
            ad=ad,
            job_source=job_source,
        )

    def set_profile_name(self, conversation_id: int, profile_name: str) -> dict:
        now = self.timestamp()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE conversations
                SET profile_name = ?, updated_at = ?
                WHERE id = ?
                """,
                (profile_name, now, conversation_id),
            )
            row = conn.execute(
                "SELECT customer_id FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if row is not None:
                conn.execute(
                    """
                    UPDATE customers
                    SET name = ?, updated_at = ?
                    WHERE id = ? AND (name IS NULL OR name = '')
                    """,
                    (profile_name, now, row["customer_id"]),
                )
            return _conversation(conn, conversation_id)

    def attach_ad(self, conversation_id: int, ad: dict) -> dict:
        now = self.timestamp()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE conversations
                SET ad_id = ?, campaign = ?, ad_headline = ?, ad_source = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    ad.get("ad_id"),
                    ad.get("campaign"),
                    ad.get("headline"),
                    ad.get("source"),
                    now,
                    conversation_id,
                ),
            )
            return _conversation(conn, conversation_id)

    def set_status(self, conversation_id: int, status: str) -> dict:
        now = self.timestamp()
        with self.connect() as conn:
            conn.execute(
                "UPDATE conversations SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, conversation_id),
            )
            return _conversation(conn, conversation_id)

    def set_active_job(self, conversation_id: int, job_id: int) -> None:
        now = self.timestamp()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE conversations
                SET active_job_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (job_id, now, conversation_id),
            )

    def add_message(
        self,
        conversation_id: int,
        direction: str,
        body: str,
        provider_message_id: str | None,
        job_id: int | None = None,
    ) -> dict:
        now = self.timestamp()
        with self.connect() as conn:
            if job_id is None:
                row = conn.execute(
                    "SELECT active_job_id FROM conversations WHERE id = ?",
                    (conversation_id,),
                ).fetchone()
                job_id = row["active_job_id"] if row else None
            cursor = conn.execute(
                """
                INSERT INTO messages (
                    conversation_id, job_id, direction, body, provider_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (conversation_id, job_id, direction, body, provider_message_id, now),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
            stored = conn.execute(
                "SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(stored)

    def get_message(self, message_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_message_by_provider_id(self, provider_message_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE provider_message_id = ?",
                (provider_message_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_messages(
        self, conversation_id: int | None = None, *, job_id: int | None = None
    ) -> list[dict]:
        with self.connect() as conn:
            if job_id is not None:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE job_id = ? ORDER BY id ASC",
                    (job_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM messages
                    WHERE conversation_id = ?
                    ORDER BY id ASC
                    """,
                    (conversation_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def job_has_inbound(self, job_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM messages
                WHERE job_id = ? AND direction = 'in'
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        return row is not None

    def last_inbound_at(self, conversation_id: int) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT created_at FROM messages
                WHERE conversation_id = ? AND direction = 'in'
                ORDER BY id DESC
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return row["created_at"] if row else None

    def get_customer(self, customer_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM customers WHERE id = ?", (customer_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_customer_by_wa(self, wa_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT c.* FROM customers c
                JOIN customer_numbers n ON n.customer_id = c.id
                WHERE n.wa_id = ?
                """,
                (wa_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_customers(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM customers ORDER BY updated_at DESC, id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_numbers(self, customer_id: int) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM customer_numbers
                WHERE customer_id = ?
                ORDER BY id ASC
                """,
                (customer_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_match_candidates(self, exclude_customer_id: int) -> list[dict]:
        """Other customers, with the addresses and job suburbs used for a soft match."""
        with self.connect() as conn:
            customers = conn.execute(
                "SELECT id, name, addresses_json FROM customers WHERE id != ?",
                (exclude_customer_id,),
            ).fetchall()
            found = []
            for customer in customers:
                addresses = _load_addresses(customer["addresses_json"])
                places = conn.execute(
                    "SELECT suburb, address FROM jobs WHERE customer_id = ?",
                    (customer["id"],),
                ).fetchall()
                for place in places:
                    if place["suburb"]:
                        addresses.append(place["suburb"])
                    if place["address"]:
                        addresses.append(place["address"])
                found.append(
                    {
                        "id": customer["id"],
                        "name": customer["name"],
                        "addresses": _unique_texts(addresses),
                    }
                )
        return found

    def customer_has_other_job(self, customer_id: int, job_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM jobs
                WHERE customer_id = ? AND id != ?
                LIMIT 1
                """,
                (customer_id, job_id),
            ).fetchone()
        return row is not None

    def note_customer_facts(
        self,
        customer_id: int,
        *,
        contact_name: str | None,
        profile_name: str | None,
        suburb: str | None,
        address: str | None,
    ) -> None:
        now = self.timestamp()
        with self.connect() as conn:
            customer = conn.execute(
                "SELECT * FROM customers WHERE id = ?", (customer_id,)
            ).fetchone()
            if customer is None:
                return
            name = customer["name"]
            confirmed = int(customer["name_confirmed"] or 0)
            if contact_name:
                same = _same_text(name, contact_name)
                profile_only = bool(profile_name) and name == profile_name and not same
                if not confirmed or not name or profile_only or same:
                    if not confirmed or not name or profile_only:
                        name = contact_name
                    confirmed = 1
            addresses = _union_addresses(
                customer["addresses_json"],
                [item for item in (address, suburb) if item],
            )
            conn.execute(
                """
                UPDATE customers
                SET name = ?, name_confirmed = ?, addresses_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (name, confirmed, json.dumps(addresses), now, customer_id),
            )

    def import_client(
        self,
        *,
        name: str,
        phones: list[str],
        address: str | None,
        notes: str | None,
    ) -> str:
        """Create or update one trusted client. Returns `created` or `updated`."""
        if not name:
            raise ValueError("Name is missing")
        normalised = []
        for phone in phones:
            number = normalise_au_phone(phone)
            if number is None:
                raise ValueError(f"Could not read phone {phone!r}")
            normalised.append(number)
        phones = _unique_texts(normalised)
        if not phones:
            raise ValueError("No usable phone number")
        now = self.timestamp()
        with self.connect() as conn:
            found: list[int] = []
            for phone in phones:
                row = conn.execute(
                    "SELECT customer_id FROM customer_numbers WHERE wa_id = ?",
                    (phone,),
                ).fetchone()
                if row is not None:
                    found.append(int(row["customer_id"]))
            owners = set(found)
            if len(owners) > 1:
                raise ValueError("Those phones already belong to different customers")
            if not owners:
                cursor = conn.execute(
                    """
                    INSERT INTO customers (
                        name, name_confirmed, trusted, addresses_json, notes,
                        created_at, updated_at
                    ) VALUES (?, 1, 1, ?, ?, ?, ?)
                    """,
                    (name, json.dumps(_clean_addresses([address])), notes, now, now),
                )
                customer_id = cursor.lastrowid
                for phone in phones:
                    conn.execute(
                        """
                        INSERT INTO customer_numbers (customer_id, wa_id, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (customer_id, phone, now),
                    )
                return "created"
            customer_id = owners.pop()
            current = conn.execute(
                "SELECT * FROM customers WHERE id = ?", (customer_id,)
            ).fetchone()
            addresses = _union_addresses(
                current["addresses_json"], [address] if address else []
            )
            conn.execute(
                """
                UPDATE customers
                SET name = ?, name_confirmed = 1, trusted = 1,
                    addresses_json = ?, notes = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    current["name"] or name,
                    json.dumps(addresses),
                    _merge_notes(current["notes"], notes),
                    now,
                    customer_id,
                ),
            )
            for phone in phones:
                row = conn.execute(
                    "SELECT customer_id FROM customer_numbers WHERE wa_id = ?",
                    (phone,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO customer_numbers (customer_id, wa_id, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (customer_id, phone, now),
                    )
            return "updated"

    def get_job(self, job_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(_JOB_SELECT + " WHERE j.id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def get_lead(self, lead_id: int) -> dict | None:
        return self.get_job(lead_id)

    def get_open_job(self, conversation_id: int) -> dict | None:
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            return None
        active_id = conversation.get("active_job_id")
        if active_id:
            active = self.get_job(active_id)
            if active and active["job_status"] in OPEN_JOB_STATUSES:
                return active
        with self.connect() as conn:
            row = conn.execute(
                _JOB_SELECT
                + """
                WHERE j.conversation_id = ?
                  AND j.status IN ('open', 'quoted', 'booked')
                ORDER BY j.updated_at DESC, j.id DESC
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_lead_for_conversation(self, conversation_id: int) -> dict | None:
        conversation = self.get_conversation(conversation_id)
        if conversation and conversation.get("active_job_id"):
            job = self.get_job(conversation["active_job_id"])
            if job:
                return job
        with self.connect() as conn:
            row = conn.execute(
                _JOB_SELECT + " WHERE j.conversation_id = ? ORDER BY j.id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_jobs(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                _JOB_SELECT + " ORDER BY j.updated_at DESC, j.id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_leads(self) -> list[dict]:
        return self.list_jobs()

    def list_jobs_for_customer(self, customer_id: int) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                _JOB_SELECT + " WHERE j.customer_id = ? ORDER BY j.id DESC",
                (customer_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def booked_intervals(self) -> list[tuple[datetime, datetime]]:
        intervals = []
        for job in self.list_jobs():
            try:
                booking = json.loads(job.get("booking_json") or "{}")
            except json.JSONDecodeError:
                continue
            if booking.get("status") != "booked":
                continue
            start, end = booking.get("start"), booking.get("end")
            if not start or not end:
                continue
            intervals.append((_parse_iso(start), _parse_iso(end)))
        return intervals

    def save_job(self, job_id: int, values: dict) -> dict:
        now = self.timestamp()
        with self.connect() as conn:
            current = conn.execute(
                "SELECT status, booking_json, quote_json FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if current is None:
                raise RuntimeError(f"no job {job_id}")
            status = _next_status(
                current["status"],
                _load_obj(values.get("booking_json")),
                _load_obj(values.get("quote_json")),
            )
            cursor = conn.execute(
                """
                UPDATE jobs SET
                    job_type = ?,
                    suburb = ?,
                    urgency = ?,
                    description = ?,
                    preferred_time = ?,
                    contact_name = ?,
                    score = ?,
                    score_reason = ?,
                    handoff = ?,
                    handoff_reason = ?,
                    photos_json = ?,
                    voice_json = ?,
                    quote_json = ?,
                    booking_json = ?,
                    extracted_json = ?,
                    status = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    values.get("job_type"),
                    values.get("suburb"),
                    values.get("urgency"),
                    values.get("description"),
                    values.get("preferred_time"),
                    values.get("contact_name"),
                    values["score"],
                    values["score_reason"],
                    1 if values.get("handoff") else 0,
                    values.get("handoff_reason"),
                    values.get("photos_json") or "[]",
                    values.get("voice_json") or "[]",
                    values.get("quote_json") or "{}",
                    values.get("booking_json") or "{}",
                    values.get("extracted_json") or "{}",
                    status,
                    now,
                    job_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"no job {job_id}")
        job = self.get_job(job_id)
        if job is None:
            raise RuntimeError(f"job {job_id} disappeared")
        return job

    def save_lead(self, conversation_id: int, values: dict) -> dict:
        conversation = self.get_conversation(conversation_id)
        if conversation is None or not conversation.get("active_job_id"):
            raise RuntimeError(f"no job for conversation {conversation_id}")
        return self.save_job(conversation["active_job_id"], values)

    def set_job_status(self, job_id: int, status: str) -> dict:
        if status not in JOB_STATUSES:
            raise ValueError(f"Unknown job status {status}")
        now = self.timestamp()
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, job_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(job_id)
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def set_soft_match(
        self,
        job_id: int,
        *,
        possible_customer_id: int | None,
        asked: bool,
        answer: str | None,
        address_prompted: bool,
    ) -> None:
        now = self.timestamp()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE jobs SET
                    possible_customer_id = ?,
                    prior_client_asked = ?,
                    prior_client_answer = ?,
                    address_prompted = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    possible_customer_id,
                    1 if asked else 0,
                    answer if answer in {"yes", "no"} else None,
                    1 if address_prompted else 0,
                    now,
                    job_id,
                ),
            )

    def link_number(self, job_id: int, target_customer_id: int) -> dict:
        """Move this number, its thread, and its jobs onto an existing customer."""
        now = self.timestamp()
        with self.connect() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            target = conn.execute(
                "SELECT * FROM customers WHERE id = ?", (target_customer_id,)
            ).fetchone()
            if target is None:
                raise KeyError(target_customer_id)
            conversation = conn.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (job["conversation_id"],),
            ).fetchone()
            source_id = job["customer_id"]
            if source_id != target_customer_id:
                source = conn.execute(
                    "SELECT * FROM customers WHERE id = ?", (source_id,)
                ).fetchone()
                conn.execute(
                    "UPDATE customer_numbers SET customer_id = ? WHERE wa_id = ?",
                    (target_customer_id, conversation["wa_id"]),
                )
                conn.execute(
                    """
                    UPDATE conversations
                    SET customer_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (target_customer_id, now, conversation["id"]),
                )
                conn.execute(
                    """
                    UPDATE jobs
                    SET customer_id = ?, possible_customer_id = NULL, updated_at = ?
                    WHERE conversation_id = ?
                    """,
                    (target_customer_id, now, conversation["id"]),
                )
                addresses = _union_addresses(
                    target["addresses_json"],
                    _load_addresses(source["addresses_json"]) if source else [],
                )
                conn.execute(
                    """
                    UPDATE customers
                    SET name = ?, name_confirmed = ?, trusted = 1,
                        addresses_json = ?, notes = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        target["name"] or (source["name"] if source else None),
                        1
                        if target["name_confirmed"] or (source and source["name_confirmed"])
                        else 0,
                        json.dumps(addresses),
                        _merge_notes(target["notes"], source["notes"] if source else None),
                        now,
                        target_customer_id,
                    ),
                )
                leftover_numbers = conn.execute(
                    "SELECT COUNT(*) AS n FROM customer_numbers WHERE customer_id = ?",
                    (source_id,),
                ).fetchone()["n"]
                leftover_jobs = conn.execute(
                    "SELECT COUNT(*) AS n FROM jobs WHERE customer_id = ?",
                    (source_id,),
                ).fetchone()["n"]
                if leftover_numbers == 0 and leftover_jobs == 0:
                    conn.execute("DELETE FROM customers WHERE id = ?", (source_id,))
            else:
                conn.execute(
                    """
                    UPDATE jobs
                    SET possible_customer_id = NULL, updated_at = ?
                    WHERE conversation_id = ?
                    """,
                    (now, conversation["id"]),
                )
                conn.execute(
                    "UPDATE customers SET trusted = 1, updated_at = ? WHERE id = ?",
                    (now, target_customer_id),
                )
        linked = self.get_job(job_id)
        if linked is None:
            raise KeyError(job_id)
        return linked

    def split_job(self, job_id: int, message_ids: list[int]) -> dict:
        """Move messages onto a new open job for the same customer and number."""
        if not message_ids:
            raise ValueError("Choose a message to split off")
        now = self.timestamp()
        with self.connect() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            placeholders = ",".join("?" for _ in message_ids)
            rows = conn.execute(
                f"""
                SELECT id FROM messages
                WHERE id IN ({placeholders}) AND job_id = ?
                """,
                (*message_ids, job_id),
            ).fetchall()
            if len(rows) != len(set(message_ids)):
                raise ValueError("That message is not on this job")
            new_id = _insert_job(
                conn,
                customer_id=job["customer_id"],
                conversation_id=job["conversation_id"],
                source=job["source"] if job["source"] in {
                    "ad", "organic", "website", "qr", "repeat"
                } else "organic",
                ad={},
                contact_name=None,
                suburb=None,
                address=None,
                now=now,
                score_reason="Split off the previous job.",
            )
            conn.execute(
                f"UPDATE messages SET job_id = ? WHERE id IN ({placeholders})",
                (new_id, *message_ids),
            )
            conn.execute(
                """
                UPDATE conversations
                SET active_job_id = ?, status = 'open', updated_at = ?
                WHERE id = ?
                """,
                (new_id, now, job["conversation_id"]),
            )
        created = self.get_job(new_id)
        if created is None:
            raise RuntimeError("split job disappeared")
        return created

    def reassign_message(self, message_id: int, target_job_id: int) -> dict:
        with self.connect() as conn:
            message = conn.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if message is None:
                raise KeyError(message_id)
            target = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (target_job_id,)
            ).fetchone()
            if target is None:
                raise KeyError(target_job_id)
            if message["job_id"]:
                source = conn.execute(
                    "SELECT customer_id FROM jobs WHERE id = ?",
                    (message["job_id"],),
                ).fetchone()
                source_customer = source["customer_id"] if source else None
            else:
                source_customer = None
            if source_customer is None:
                conversation = conn.execute(
                    "SELECT customer_id FROM conversations WHERE id = ?",
                    (message["conversation_id"],),
                ).fetchone()
                source_customer = conversation["customer_id"] if conversation else None
            if source_customer != target["customer_id"]:
                raise ValueError("That job belongs to a different customer")
            conn.execute(
                "UPDATE messages SET job_id = ? WHERE id = ?",
                (target_job_id, message_id),
            )
        job = self.get_job(target_job_id)
        if job is None:
            raise KeyError(target_job_id)
        return job


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        _ensure_message_job_index(conn)
        return
    # executescript above may already have committed the CREATE statements.
    # Copy legacy rows in one transaction so a failure does not duplicate them.
    conn.execute("BEGIN")
    try:
        _ensure_legacy_lead_columns(conn)
        _add_column(conn, "conversations", "customer_id", "INTEGER")
        _add_column(conn, "conversations", "active_job_id", "INTEGER")
        _add_column(conn, "messages", "job_id", "INTEGER")
        if _table_exists(conn, "leads"):
            job_count = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
            if job_count == 0:
                _copy_legacy(conn)
            conn.execute("DROP TABLE leads")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    _ensure_message_job_index(conn)


def _copy_legacy(conn: sqlite3.Connection) -> None:
    conversations = conn.execute("SELECT * FROM conversations").fetchall()
    copied = 0
    for conversation in conversations:
        lead = None
        if _table_exists(conn, "leads"):
            lead = conn.execute(
                "SELECT * FROM leads WHERE conversation_id = ?",
                (conversation["id"],),
            ).fetchone()
        name = None
        suburb = None
        confirmed = 0
        if lead is not None and lead["contact_name"]:
            name = lead["contact_name"]
            confirmed = 1
        elif conversation["profile_name"]:
            name = conversation["profile_name"]
        if lead is not None and lead["suburb"]:
            suburb = lead["suburb"]
        addresses = json.dumps([suburb] if suburb else [])
        cursor = conn.execute(
            """
            INSERT INTO customers (
                name, name_confirmed, trusted, addresses_json, notes,
                created_at, updated_at
            ) VALUES (?, ?, 1, ?, NULL, ?, ?)
            """,
            (
                name,
                confirmed,
                addresses,
                conversation["created_at"],
                conversation["updated_at"],
            ),
        )
        customer_id = cursor.lastrowid
        conn.execute(
            """
            INSERT INTO customer_numbers (customer_id, wa_id, created_at)
            VALUES (?, ?, ?)
            """,
            (customer_id, conversation["wa_id"], conversation["created_at"]),
        )
        conn.execute(
            "UPDATE conversations SET customer_id = ? WHERE id = ?",
            (customer_id, conversation["id"]),
        )
        if lead is None:
            job_id = _insert_job(
                conn,
                customer_id=customer_id,
                conversation_id=conversation["id"],
                source=_legacy_source(conversation),
                ad=_legacy_ad(conversation),
                contact_name=None,
                suburb=None,
                address=None,
                now=conversation["created_at"],
            )
        else:
            job_id = int(lead["id"])
            conn.execute(
                """
                INSERT INTO jobs (
                    id, customer_id, conversation_id, status, source,
                    address, job_type, suburb, urgency, description,
                    preferred_time, contact_name, score, score_reason,
                    handoff, handoff_reason, photos_json, voice_json,
                    quote_json, booking_json, extracted_json,
                    ad_id, campaign, ad_headline, ad_source,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    NULL, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?
                )
                """,
                (
                    job_id,
                    customer_id,
                    conversation["id"],
                    _legacy_job_status(lead),
                    _legacy_source(conversation),
                    lead["job_type"],
                    lead["suburb"],
                    lead["urgency"],
                    lead["description"],
                    lead["preferred_time"],
                    lead["contact_name"],
                    lead["score"],
                    lead["score_reason"],
                    lead["handoff"],
                    lead["handoff_reason"],
                    lead["photos_json"] or "[]",
                    lead["voice_json"] or "[]",
                    lead["quote_json"] or "{}",
                    lead["booking_json"] or "{}",
                    lead["extracted_json"] or "{}",
                    conversation["ad_id"],
                    conversation["campaign"],
                    conversation["ad_headline"],
                    conversation["ad_source"],
                    lead["created_at"],
                    lead["updated_at"],
                ),
            )
        conn.execute(
            "UPDATE conversations SET active_job_id = ? WHERE id = ?",
            (job_id, conversation["id"]),
        )
        conn.execute(
            """
            UPDATE messages SET job_id = ?
            WHERE conversation_id = ? AND job_id IS NULL
            """,
            (job_id, conversation["id"]),
        )
        copied += 1
    if copied:
        logger.info("migrated %s conversation(s) to customers and jobs", copied)


def _legacy_source(conversation: sqlite3.Row) -> str:
    if conversation["ad_id"]:
        return "ad"
    raw = (conversation["source"] or "").lower()
    if raw in {"ad", "organic", "website", "qr", "repeat"}:
        return raw
    return "organic"


def _legacy_ad(conversation: sqlite3.Row) -> dict:
    if not conversation["ad_id"] and not conversation["ad_headline"]:
        return {}
    return {
        "ad_id": conversation["ad_id"],
        "campaign": conversation["campaign"],
        "headline": conversation["ad_headline"],
        "source": conversation["ad_source"],
    }


def _legacy_job_status(lead: sqlite3.Row) -> str:
    booking = _load_obj(lead["booking_json"])
    quote = _load_obj(lead["quote_json"])
    if booking.get("status") == "booked":
        return "booked"
    if quote.get("status") == "sent":
        return "quoted"
    return "open"


def _insert_job(
    conn: sqlite3.Connection,
    *,
    customer_id: int,
    conversation_id: int,
    source: str,
    ad: dict,
    contact_name: str | None,
    suburb: str | None,
    address: str | None,
    now: str,
    score_reason: str = "No job type yet.",
) -> int:
    if source not in {"ad", "organic", "website", "qr", "repeat"}:
        source = "organic"
    cursor = conn.execute(
        """
        INSERT INTO jobs (
            customer_id, conversation_id, status, source, address,
            job_type, suburb, contact_name, score, score_reason, handoff,
            ad_id, campaign, ad_headline, ad_source,
            extracted_json, created_at, updated_at
        ) VALUES (
            ?, ?, 'open', ?, ?,
            NULL, ?, ?, 'cold', ?, 0,
            ?, ?, ?, ?,
            '{}', ?, ?
        )
        """,
        (
            customer_id,
            conversation_id,
            source,
            address,
            suburb,
            contact_name,
            score_reason,
            ad.get("ad_id"),
            ad.get("campaign"),
            ad.get("headline"),
            ad.get("source"),
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def _ensure_number(conn, customer_id: int, wa_id: str, now: str) -> None:
    row = conn.execute(
        "SELECT customer_id FROM customer_numbers WHERE wa_id = ?",
        (wa_id,),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO customer_numbers (customer_id, wa_id, created_at)
            VALUES (?, ?, ?)
            """,
            (customer_id, wa_id, now),
        )
        return
    if int(row["customer_id"]) != int(customer_id):
        raise ValueError(f"WhatsApp id {wa_id} already belongs to another customer")


def _ensure_legacy_lead_columns(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "leads"):
        return
    existing = {row[1] for row in conn.execute("PRAGMA table_info(leads)")}
    for name, typedef in _LEGACY_LEAD_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {name} {typedef}")


def _ensure_message_job_index(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "job_id" not in columns:
        return
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_job ON messages(job_id)"
    )


def _add_column(conn, table: str, name: str, typedef: str) -> None:
    if not _table_exists(conn, table):
        return
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typedef}")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _conversation(conn: sqlite3.Connection, conversation_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    if row is None:
        raise KeyError(conversation_id)
    return dict(row)


def _job(conn: sqlite3.Connection, job_id: int) -> dict:
    row = conn.execute(_JOB_SELECT + " WHERE j.id = ?", (job_id,)).fetchone()
    if row is None:
        raise KeyError(job_id)
    return dict(row)


def _next_status(current: str, booking: dict, quote: dict) -> str:
    if current in {"completed", "cancelled"}:
        return current
    if booking.get("status") == "booked":
        return "booked"
    if quote.get("status") == "sent":
        return "quoted"
    if current == "quoted":
        return "quoted"
    return "open"


def _load_obj(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _load_addresses(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _clean_addresses(items: list[str | None]) -> list[str]:
    return _unique_texts([item for item in items if item])


def _union_addresses(existing, extra: list[str]) -> list[str]:
    return _unique_texts(_load_addresses(existing) + [item for item in extra if item])


def _unique_texts(items) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append(text)
    return found


def _merge_notes(current: str | None, extra: str | None) -> str | None:
    current_text = (current or "").strip()
    extra_text = (extra or "").strip()
    if not extra_text:
        return current_text or None
    if not current_text:
        return extra_text
    if extra_text.casefold() in current_text.casefold():
        return current_text
    return f"{current_text}\n{extra_text}"


def _same_text(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return " ".join(left.casefold().split()) == " ".join(right.casefold().split())


def _parse_iso(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
