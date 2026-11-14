"""Keyword extraction for the offline qualifier.

This is a stand-in so the simulator runs with no API key. It is not the
product's reader — Claude is. Patterns come from the business config where
they can (job types and suburbs) so another trade does not need a new parser.
"""

from __future__ import annotations

import re

from app.business import Business, load_business

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
    "calling",
    "needing",
    "having",
    "getting",
    "living",
    "based",
    "located",
    "after",
    "before",
    "from",
    "with",
    "just",
    "really",
    "very",
    "not",
    "in",
    "at",
    "near",
    "the",
    "a",
    "to",
}

_SUBURB_INTRO = re.compile(
    r"\b(?:in|at|near|around|from)\s+([A-Za-z][A-Za-z]+(?:\s+[A-Za-z][A-Za-z]+)?)\b"
)
_SUBURB_STOP = {"the", "my", "our", "your", "a", "an", "this", "that", "some", "town"}

_EMERGENCY = (
    "emergency",
    "no power",
    "power is out",
    "power's out",
    "powers out",
    "power out",
    "sparking",
    "sparks",
    "burning smell",
    "smell burning",
    "smells like burning",
    "electric shock",
    "got a shock",
    "right now",
    "smoke",
)
_PLANNED = (
    "not urgent",
    "no rush",
    "no hurry",
    "whenever",
    "when you can",
    "next week",
    "next month",
)
_URGENT = (
    "urgent",
    "asap",
    "as soon as",
    "today",
    "this afternoon",
    "straight away",
    "straightaway",
)

_PREFERRED = (
    r"\b(tomorrow(?:\s+(?:morning|arvo|afternoon|evening))?)\b",
    r"\b(this\s+(?:morning|afternoon|arvo|evening|week))\b",
    r"\b(next\s+(?:week|monday|tuesday|wednesday|thursday|friday))\b",
    r"\b((?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?:\s+(?:morning|afternoon|arvo))?)\b",
    r"\b(morning|afternoon|arvo)\b",
)


def extract_job_type(text: str, business: Business | None = None) -> str | None:
    business = business or load_business()
    lowered = text.casefold()
    matches: list[tuple[int, int, str, bool]] = []
    for job in business.job_types:
        for alias in job.aliases:
            match = re.search(rf"\b{re.escape(alias.casefold())}\b", lowered)
            if match:
                matches.append((match.start(), -len(alias), job.id, job.emergency))
    if not matches:
        return None
    if _urgency_label(lowered) == "emergency":
        emergency = [item for item in matches if item[3]]
        if emergency:
            emergency.sort()
            return emergency[0][2]
    matches.sort()
    return matches[0][2]


def extract_suburb(text: str, business: Business | None = None) -> str | None:
    business = business or load_business()
    lowered = text.casefold()
    hits: list[tuple[int, int, str]] = []
    for suburb in sorted(business.suburbs, key=len, reverse=True):
        match = re.search(rf"\b{re.escape(suburb.casefold())}\b", lowered)
        if match:
            hits.append((match.start(), -len(suburb), suburb))
    if hits:
        hits.sort()
        return hits[0][2]
    match = _SUBURB_INTRO.search(text)
    if not match:
        return None
    parts = match.group(1).split()
    if parts[0].casefold() in _SUBURB_STOP:
        return None
    return " ".join(part.capitalize() for part in parts)


def extract_urgency(text: str) -> str | None:
    return _urgency_label(text.casefold())


def extract_description(text: str) -> str | None:
    cleaned = " ".join(text.split())
    if len(cleaned.split()) < 6:
        return None
    if _NAME.fullmatch(cleaned):
        return None
    return cleaned


def extract_preferred_time(text: str) -> str | None:
    for pattern in _PREFERRED:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return " ".join(match.group(1).split())
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


def extract_facts(text: str, business: Business | None = None) -> dict:
    business = business or load_business()
    return {
        "job_type": extract_job_type(text, business),
        "suburb": extract_suburb(text, business),
        "urgency": extract_urgency(text),
        "description": extract_description(text),
        "preferred_time": extract_preferred_time(text),
        "contact_name": extract_name(text),
    }


def _urgency_label(lowered: str) -> str | None:
    if any(phrase in lowered for phrase in _EMERGENCY):
        return "emergency"
    if any(phrase in lowered for phrase in _PLANNED):
        return "planned"
    if any(phrase in lowered for phrase in _URGENT):
        return "urgent"
    return None
