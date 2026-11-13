"""Job score and handoff rules.

The model extracts facts. This module decides hot / warm / cold from urgency
and whether the suburb is in the configured service area. The rules are
ordinary code so a change in who counts as a hot job does not require a new
prompt, and the tests can pin the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.business import Business, load_business

FACT_KEYS = (
    "job_type",
    "suburb",
    "urgency",
    "description",
    "preferred_time",
    "contact_name",
)

_URGENCY = {"emergency", "urgent", "planned"}

# Phrases, not single words, so "someone in my family" does not trip handoff.
_HUMAN_PHRASES = (
    "real person",
    "real human",
    "real agent",
    "talk to a person",
    "speak to a person",
    "speak to someone",
    "talk to someone",
    "talk to an agent",
    "speak to an agent",
    "speak with a person",
    "talk with a person",
    "human agent",
    "speak to the electrician",
    "talk to the electrician",
    "talk to the tradie",
    "speak to the tradie",
    "call me",
)


@dataclass(frozen=True)
class Score:
    label: str  # hot, warm, or cold
    reason: str


def wants_human(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(phrase in lowered for phrase in _HUMAN_PHRASES)


def merge_facts(previous: dict | None, update: dict) -> dict:
    """Keep stored facts. Overwrite a field only when the new value is present."""
    merged = {key: (previous or {}).get(key) for key in FACT_KEYS}
    for key in FACT_KEYS:
        value = update.get(key)
        if value is None or value == "":
            continue
        merged[key] = value
    return merged


def canonical_facts(facts: dict, business: Business | None = None) -> dict:
    """Store job types and suburbs in the spelling the config uses."""
    business = business or load_business()
    merged = merge_facts({}, facts)
    resolved = business.resolve_job_type(merged.get("job_type"))
    if resolved:
        merged["job_type"] = resolved
    elif merged.get("job_type"):
        merged["job_type"] = str(merged["job_type"]).strip()
    suburb = business.canonical_suburb(merged.get("suburb"))
    merged["suburb"] = suburb
    merged["urgency"] = normalise_urgency(merged.get("urgency"))
    for key in ("description", "preferred_time", "contact_name"):
        text = merged.get(key)
        merged[key] = str(text).strip() if text else None
    return merged


def normalise_urgency(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text in _URGENCY:
        return text
    if text in {"asap", "today", "now", "immediately"}:
        return "urgent"
    if text in {"routine", "flexible", "whenever", "no rush"}:
        return "planned"
    return None


def score_lead(facts: dict, business: Business | None = None) -> Score:
    """Score one job.

    Cold: no configured job type yet.
    Warm: a job type is known, but the suburb is outside the service area, or
    urgency, description, or a name is still missing.
    Hot: a configured job type, a suburb inside the service area, and either an
    emergency or the full checklist (urgency, description, and a name).
    """
    business = business or load_business()
    job_id = business.resolve_job_type(facts.get("job_type"))
    raw_job = _text(facts.get("job_type"))
    suburb = business.canonical_suburb(facts.get("suburb")) if facts.get("suburb") else None
    in_area = business.in_area(suburb)
    urgency = normalise_urgency(facts.get("urgency"))
    description = _text(facts.get("description"))
    name = _text(facts.get("contact_name"))

    if not raw_job:
        return Score("cold", "No job type yet.")
    if not job_id:
        return Score("warm", "The job isn't one of the configured types yet.")
    if suburb and not in_area:
        return Score(
            "warm",
            "Job type is known, but the suburb is outside the service area.",
        )
    if urgency == "emergency" and in_area:
        return Score(
            "hot",
            "Emergency in the service area with a known job type.",
        )
    if in_area and urgency and description and name:
        return Score(
            "hot",
            "Job type, in-area suburb, urgency, description, and name are on file.",
        )
    return Score(
        "warm",
        "Job type is known; suburb, urgency, description, or name is still missing.",
    )


def handoff_decision(score: Score, asked_for_human: bool) -> tuple[bool, str | None]:
    """Hand to a person when the job is hot, or when they ask, at any score."""
    if score.label == "hot":
        return True, "hot_lead"
    if asked_for_human:
        return True, "human_requested"
    return False, None


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
