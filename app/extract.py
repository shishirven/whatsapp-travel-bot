"""Keyword extraction for the offline qualifier.

This is a stand-in so the simulator runs with no API key. It is not the
product's reader — Claude is. The patterns are deliberately small and tested.
"""

from __future__ import annotations

import re

from app.knowledge import destination_aliases

_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)"
)

# A range is a dash or the word "to", not a character class (that would treat
# the letters t and o as separators).
_RANGE = r"(?:\s*(?:-|–|to)\s*\d{1,2})?"

# Month phrases before bare numeric ranges, so "June 12-19 2026" is not cut to "12-19".
_DATE_PATTERNS = (
    rf"\b({_MONTH}\s+\d{{1,2}}{_RANGE}(?:,?\s+\d{{4}})?)\b",
    rf"\b(\d{{1,2}}{_RANGE}\s+{_MONTH}(?:,?\s+\d{{4}})?)\b",
    rf"\b((?:next|this)\s+(?:week|month|weekend|summer|spring|fall|autumn|winter))\b",
    rf"\b(in\s+{_MONTH}(?:\s+\d{{4}})?)\b",
    rf"\b({_MONTH}\s+\d{{4}})\b",
    rf"\b(\d{{1,2}}[/-]\d{{1,2}}(?:[/-]\d{{2,4}})?)\b",
)

_NAME = re.compile(
    r"\b(?:my name is|i am|i'm|this is)\s+([A-Za-z][a-z]+(?:\s+[A-Za-z][a-z]+)?)",
    re.IGNORECASE,
)
_NAME_STOP = {
    "interested",
    "looking",
    "hoping",
    "going",
    "planning",
    "wondering",
    "here",
    "good",
    "fine",
    "trying",
    "thinking",
    "coming",
    "traveling",
    "travelling",
    "to",
    "the",
    "a",
    "not",
    "just",
}

_GROUP_PATTERNS = (
    r"\b(\d{1,2})\s*(?:people|adults|travellers|travelers|guests|pax)\b",
    r"\bgroup of\s+(\d{1,2})\b",
    r"\b(?:we are|we're)\s+(\d{1,2})\b",
    r"\bfamily of\s+(\d{1,2})\b",
)

_BUDGET_PATTERNS = (
    r"\$\s*(\d{2,7}(?:,\d{3})*)",
    r"\b(\d{2,7}(?:,\d{3})*)\s*(?:usd|dollars?)\b",
    r"\b(?:budget|under|up to)\s*(?:of|is|around|about)?\s*\$?\s*(\d{2,7}(?:,\d{3})*)",
)


def extract_destination(text: str) -> str | None:
    lowered = text.lower()
    hits: list[tuple[int, int, str]] = []
    for alias, destination in destination_aliases():
        match = re.search(rf"\b{re.escape(alias)}\b", lowered)
        if match:
            hits.append((match.start(), -len(alias), destination))
    if not hits:
        return None
    hits.sort()
    return hits[0][2]


def extract_dates(text: str) -> str | None:
    for pattern in _DATE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return " ".join(match.group(1).split())
    return None


def extract_group_size(text: str) -> int | None:
    lowered = text.lower()
    if re.search(r"\bsolo\b", lowered):
        return 1
    if re.search(r"\b(couple|two of us|just us two)\b", lowered):
        return 2
    for pattern in _GROUP_PATTERNS:
        match = re.search(pattern, lowered)
        if not match:
            continue
        size = int(match.group(1))
        if 1 <= size <= 50:
            return size
    return None


def extract_budget(text: str) -> float | None:
    for pattern in _BUDGET_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        value = float(match.group(1).replace(",", ""))
        if value >= 50:
            return value
    return None


def extract_name(text: str) -> str | None:
    match = _NAME.search(text)
    if not match:
        return None
    parts = match.group(1).split()
    if any(part.lower() in _NAME_STOP for part in parts):
        return None
    if any(len(part) < 2 for part in parts):
        return None
    return " ".join(part.capitalize() for part in parts)


def extract_facts(text: str) -> dict:
    return {
        "destination": extract_destination(text),
        "travel_dates": extract_dates(text),
        "group_size": extract_group_size(text),
        "budget_usd": extract_budget(text),
        "contact_name": extract_name(text),
    }
