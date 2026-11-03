"""SQLite storage for conversations, messages, and one lead per conversation.

The connection is opened per call so FastAPI's threadpool does not share a
sqlite3 connection across threads.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_id TEXT NOT NULL UNIQUE,
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

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
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

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL UNIQUE REFERENCES conversations(id),
    destination TEXT,
    travel_dates TEXT,
    group_size INTEGER,
    budget_usd REAL,
    contact_name TEXT,
    score TEXT NOT NULL,
    score_reason TEXT NOT NULL,
    handoff INTEGER NOT NULL DEFAULT 0,
    handoff_reason TEXT,
    extracted_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_LEAD_SELECT = """
SELECT
    l.id, l.conversation_id, l.destination, l.travel_dates, l.group_size,
    l.budget_usd, l.contact_name, l.score, l.score_reason, l.handoff,
    l.handoff_reason, l.extracted_json, l.created_at, l.updated_at,
    c.wa_id, c.profile_name, c.source, c.ad_id, c.campaign, c.ad_headline,
    c.ad_source, c.status AS conversation_status,
    (
        SELECT body FROM messages m
        WHERE m.conversation_id = c.id
        ORDER BY m.id DESC
        LIMIT 1
    ) AS last_message
FROM leads l
JOIN conversations c ON c.id = l.conversation_id
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(_SCHEMA)

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
    ) -> dict:
        now = utcnow()
        ad = ad or {}
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO conversations (
                    wa_id, profile_name, source, ad_id, campaign, ad_headline,
                    ad_source, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (
                    wa_id,
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
            conn.execute(
                """
                INSERT INTO leads (
                    conversation_id, score, score_reason, handoff,
                    extracted_json, created_at, updated_at
                ) VALUES (?, 'cold', ?, 0, '{}', ?, ?)
                """,
                (conversation_id, "No destination yet.", now, now),
            )
            return _conversation(conn, conversation_id)

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
    ) -> dict:
        existing = self.get_conversation_by_wa(wa_id)
        if existing:
            if profile_name and not existing["profile_name"]:
                return self.set_profile_name(existing["id"], profile_name)
            return existing
        return self.create_conversation(
            wa_id=wa_id, profile_name=profile_name, source=source
        )

    def set_profile_name(self, conversation_id: int, profile_name: str) -> dict:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE conversations
                SET profile_name = ?, updated_at = ?
                WHERE id = ?
                """,
                (profile_name, now, conversation_id),
            )
            return _conversation(conn, conversation_id)

    def attach_ad(self, conversation_id: int, ad: dict) -> dict:
        now = utcnow()
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
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                "UPDATE conversations SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, conversation_id),
            )
            return _conversation(conn, conversation_id)

    def add_message(
        self,
        conversation_id: int,
        direction: str,
        body: str,
        provider_message_id: str | None,
    ) -> dict:
        now = utcnow()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO messages (
                    conversation_id, direction, body, provider_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (conversation_id, direction, body, provider_message_id, now),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
            row = conn.execute(
                "SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(row)

    def get_message_by_provider_id(self, provider_message_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE provider_message_id = ?",
                (provider_message_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_messages(self, conversation_id: int) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM messages
                WHERE conversation_id = ?
                ORDER BY id ASC
                """,
                (conversation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_lead(self, lead_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(_LEAD_SELECT + " WHERE l.id = ?", (lead_id,)).fetchone()
        return dict(row) if row else None

    def get_lead_for_conversation(self, conversation_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                _LEAD_SELECT + " WHERE c.id = ?", (conversation_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_leads(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(_LEAD_SELECT + " ORDER BY l.updated_at DESC, l.id DESC").fetchall()
        return [dict(row) for row in rows]

    def save_lead(self, conversation_id: int, values: dict) -> dict:
        now = utcnow()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE leads SET
                    destination = ?,
                    travel_dates = ?,
                    group_size = ?,
                    budget_usd = ?,
                    contact_name = ?,
                    score = ?,
                    score_reason = ?,
                    handoff = ?,
                    handoff_reason = ?,
                    extracted_json = ?,
                    updated_at = ?
                WHERE conversation_id = ?
                """,
                (
                    values.get("destination"),
                    values.get("travel_dates"),
                    values.get("group_size"),
                    values.get("budget_usd"),
                    values.get("contact_name"),
                    values["score"],
                    values["score_reason"],
                    1 if values.get("handoff") else 0,
                    values.get("handoff_reason"),
                    values.get("extracted_json") or "{}",
                    now,
                    conversation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"no lead row for conversation {conversation_id}")
        lead = self.get_lead_for_conversation(conversation_id)
        if lead is None:
            raise RuntimeError(f"lead disappeared for conversation {conversation_id}")
        return lead


def _conversation(conn: sqlite3.Connection, conversation_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    if row is None:
        raise KeyError(conversation_id)
    return dict(row)
