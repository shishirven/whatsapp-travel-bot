"""Decide what an inbound WhatsApp message is for.

One function classifies the job source. Another decides whether a new number
might already be a client, without ever putting that client's history into
the reply. A third decides whether an open job should stay open or a new job
should start. The keyword classifier is the offline stand-in. Tests pass a
fake. The Anthropic classifier is only built when an API key is configured.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("tradie.routing")

JOB_SOURCES = ("ad", "organic", "website", "qr", "repeat")
OPEN_JOB_STATUSES = ("open", "quoted", "booked")
JOB_STATUSES = OPEN_JOB_STATUSES + ("completed", "cancelled")

PRIOR_CLIENT_QUESTION = "Have we done work for you before?"
ADDRESS_SEARCH_QUESTION = "What address did we do that work at?"

_NEW_JOB_PHRASES = (
    "new job",
    "another job",
    "different job",
    "separate job",
    "other job",
    "second job",
    "new quote",
    "start a new",
    "something different",
    "different problem",
    "another issue",
    "new issue",
    "not this job",
    "forget that job",
    "something else",
)

_STATUS_PHRASES = (
    "status",
    "any update",
    "update on",
    "where are we",
    "how's it going",
    "hows it going",
    "is it booked",
    "am i booked",
    "what time am i booked",
    "when are you coming",
    "when are you booked",
    "what's happening",
    "whats happening",
    "what time is",
    "progress",
)

_YES = {
    "yes",
    "yeah",
    "yep",
    "yup",
    "we have",
    "you have",
    "you did",
    "i think so",
}
_NO = {
    "no",
    "nope",
    "nah",
    "never",
    "you haven't",
    "you have not",
    "not yet",
    "no you haven't",
}

_INTENT_SYSTEM = (
    "You classify one WhatsApp message to a trade business that already has "
    "an open job. Reply with exactly YES if the customer is clearly asking to "
    "start a different or new job, separate from the open one. Reply with "
    "exactly NO if they are adding detail, sending a photo or voice note, "
    "choosing a time, asking for a person, or asking for status. No other words."
)


def classify_source(text: str | None, *, inbound, returning: bool, business) -> tuple[str, str | None]:
    """Return `(source, text)` with prefilled source tags removed from the text.

    A configured tag such as `(web)` or `(qr)` wins, then a Meta referral,
    then `repeat` for a number already on file, otherwise `organic`.
    """
    cleaned = text
    found = None
    if text:
        cleaned = text
        for source, tags in business.source_tags:
            for tag in tags:
                if not tag:
                    continue
                if tag.casefold() not in cleaned.casefold():
                    continue
                if found is None:
                    found = source
                cleaned = re.sub(re.escape(tag), " ", cleaned, flags=re.IGNORECASE)
        cleaned = " ".join(cleaned.split())
    if found:
        return found, cleaned
    if getattr(inbound, "ad_id", None) or getattr(inbound, "ad_headline", None):
        return "ad", cleaned
    if returning:
        return "repeat", cleaned
    return "organic", cleaned


def is_status_question(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(phrase in lowered for phrase in _STATUS_PHRASES)


def status_reply(job: dict, booking: dict, quote: dict) -> str:
    """Answer from this job only. Never include a price."""
    if booking.get("status") == "booked":
        when = booking.get("label") or "the visit we locked in"
        return (
            f"You're booked for {when}. "
            "The electrician has the rest of this job."
        )
    if booking.get("status") == "offered" and booking.get("slots"):
        return (
            "I've offered visit times for this job and I'm waiting on which one suits. "
            "Reply 1, 2, or 3."
        )
    if quote.get("status") == "sent" or (job.get("job_status") or job.get("status")) == "quoted":
        return (
            "A quote for this job is already with you. "
            "A person can walk through it if you want to talk it over."
        )
    if job.get("handoff"):
        return "This job is with the electrician. They'll reply in this chat."
    label = (job.get("job_status") or job.get("status") or "open").replace("_", " ")
    return f"This job is {label}. I'm still working from the details you've sent."


class KeywordIntentClassifier:
    """Offline stand-in. Phrase match, not a model. Tests can replace this."""

    def classify(self, text: str, job_summary: str = "") -> bool:
        lowered = (text or "").lower()
        if not lowered:
            return False
        return any(phrase in lowered for phrase in _NEW_JOB_PHRASES)


class LLMIntentClassifier:
    """One short Claude call. Constructed only when an API key exists."""

    def __init__(self, api_key: str, model: str, client=None) -> None:
        self.model = model
        if client is not None:
            self.client = client
        else:
            import anthropic

            self.client = anthropic.Anthropic(api_key=api_key)

    def classify(self, text: str, job_summary: str = "") -> bool:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=20,
            system=_INTENT_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Open job: {job_summary or 'unspecified'}\n"
                        f"Message:\n{text or ''}"
                    ),
                }
            ],
        )
        chunks: list[str] = []
        for block in getattr(response, "content", None) or []:
            if isinstance(block, dict):
                if block.get("type") == "text" and block.get("text"):
                    chunks.append(str(block["text"]))
            elif getattr(block, "type", None) == "text":
                text_block = getattr(block, "text", "") or ""
                if text_block:
                    chunks.append(str(text_block))
        return _is_yes(" ".join(chunks))


def build_intent(llm_provider: str, api_key: str, model: str):
    if llm_provider == "anthropic" and api_key:
        return LLMIntentClassifier(api_key, model)
    return KeywordIntentClassifier()


def job_summary(job: dict) -> str:
    kind = job.get("job_type") or "unknown type"
    suburb = job.get("suburb") or "unknown suburb"
    status = job.get("job_status") or job.get("status") or "open"
    return f"{kind}, {suburb}, {status}"


def norm_name(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).casefold().split())
    return text or None


def addresses_match(left: object, right: object) -> bool:
    """True when two address strings are the same place, including a suburb inside a street."""
    a = _norm_address(left)
    b = _norm_address(right)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < 4:
        return False
    return re.search(rf"\b{re.escape(short)}\b", long) is not None


def suburb_in_address(address: str | None, business) -> str | None:
    if not address:
        return None
    folded = address.casefold()
    matches = []
    for suburb in business.suburbs:
        if re.search(rf"\b{re.escape(suburb.casefold())}\b", folded):
            matches.append(suburb)
    if not matches:
        if business.in_area(address):
            return business.canonical_suburb(address)
        return None
    matches.sort(key=len, reverse=True)
    return matches[0]


def interpret_prior_answer(text: str | None) -> str | None:
    if not text:
        return None
    lowered = " ".join(text.strip().lower().rstrip(".!?").split())
    if not lowered:
        return None
    if lowered in _YES or lowered.startswith("yes ") or lowered.startswith("yeah "):
        return "yes"
    if lowered in _NO or lowered.startswith("no ") or lowered.startswith("nope "):
        return "no"
    return None


def evaluate_soft_match(
    *,
    name: str | None,
    suburb: str | None,
    address: str | None,
    text: str | None,
    asked: bool,
    answer: str | None,
    address_prompted: bool,
    candidates: list[dict],
) -> dict:
    """Flag a unique name+address match. Otherwise ask at most once, then ask for an address.

    `candidates` are other customers (`id`, `name`, `addresses`). Nothing in the
    returned question names them or their history.
    """
    result = {
        "possible_customer_id": None,
        "asked": bool(asked),
        "answer": answer if answer in {"yes", "no"} else None,
        "address_prompted": bool(address_prompted),
        "question": None,
    }
    if result["asked"] and result["answer"] is None:
        heard = interpret_prior_answer(text)
        if heard:
            result["answer"] = heard

    name_key = norm_name(name)
    if not name_key:
        return result
    named = [item for item in candidates if norm_name(item.get("name")) == name_key]
    if not named:
        return result

    haystacks = [piece for piece in (suburb, address) if piece]
    matched = []
    if haystacks:
        for candidate in named:
            stored = candidate.get("addresses") or []
            if any(addresses_match(piece, item) for piece in haystacks for item in stored):
                matched.append(candidate)
    if len(matched) == 1:
        result["possible_customer_id"] = matched[0]["id"]
        return result

    if result["answer"] == "no":
        return result
    if not result["asked"]:
        result["asked"] = True
        result["question"] = PRIOR_CLIENT_QUESTION
        return result
    if result["answer"] == "yes" and not result["address_prompted"] and not haystacks:
        result["address_prompted"] = True
        result["question"] = ADDRESS_SEARCH_QUESTION
    return result


def _is_yes(text: str) -> bool:
    token = " ".join((text or "").upper().split())
    return token.startswith("YES")


def _norm_address(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).casefold().replace(",", " ").split())
    return text or None
