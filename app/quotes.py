"""Draft price ranges.

Bands live in the business config. A draft is stored on the job for the
tradie to approve. Nothing here is sent to the customer until that happens.
"""

from __future__ import annotations

import re

from app.business import Business

_MONEY = re.compile(
    r"(\$\s?\d|\b\d[\d,]*(?:\.\d+)?\s?(?:aud|dollars)\b|\baud\s?\d)",
    re.IGNORECASE,
)
_PRICE_FALLBACK = (
    "I've noted that. The electrician confirms any pricing with you directly — "
    "I won't send a figure from this chat."
)


def draft_quote(business: Business, job_type: str | None, existing: dict | None) -> dict | None:
    """Keep a sent quote. Otherwise refresh the draft from the current job type."""
    if existing and existing.get("status") == "sent":
        return existing
    band = business.quote_for(job_type)
    if band is None:
        return None
    return {
        "job_type": band.job_type,
        "label": band.label,
        "min_aud": band.min_aud,
        "max_aud": band.max_aud,
        "basis": band.basis,
        "status": "draft",
        "sent_via": None,
    }


def customer_quote_text(
    business: Business,
    quote: dict,
    *,
    contact_name: str | None,
    suburb: str | None,
) -> str:
    who = contact_name or "there"
    where = f" in {suburb}" if suburb else ""
    low = int(quote["min_aud"])
    high = int(quote["max_aud"])
    return (
        f"Hi {who}, here's the draft range for the {quote['label']}{where}: "
        f"${low}–${high} {business.currency} ({quote['basis']}). "
        "This is a guide, not a fixed quote — the electrician confirms on site."
    )


def scrub_prices(reply: str) -> str:
    """Drop any sentence that states a price. The desk sends prices, not the bot."""
    text = (reply or "").strip()
    if not text or not _MONEY.search(text):
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = [sentence for sentence in sentences if sentence and not _MONEY.search(sentence)]
    cleaned = " ".join(sentence.strip() for sentence in kept).strip()
    return cleaned or _PRICE_FALLBACK
