"""Lead score and handoff rules.

The model extracts facts. This module decides hot / warm / cold. The rules are
ordinary code so a change in who counts as a hot lead does not require a new
prompt, and the tests can pin the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

# Cheapest sample package is $1,299. $800 is "serious enough to brief sales",
# not a quote. Change it here when the agency wants a different floor.
HOT_BUDGET_USD = 800

FACT_KEYS = ("destination", "travel_dates", "group_size", "budget_usd", "contact_name")

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
    "sales team",
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


def score_lead(facts: dict) -> Score:
    """Score one lead.

    Cold: no destination yet.
    Warm: destination is known, but the hot checklist is incomplete, or the
    budget is under HOT_BUDGET_USD.
    Hot: destination, dates, group size, budget at or above the floor, and a name.
    """
    destination = _text(facts.get("destination"))
    dates = _text(facts.get("travel_dates"))
    name = _text(facts.get("contact_name"))
    group = _number(facts.get("group_size"))
    budget = _number(facts.get("budget_usd"))
    has_group = group is not None and group >= 1
    has_budget = budget is not None and budget > 0

    if not destination:
        return Score("cold", "No destination yet.")

    complete = bool(dates and has_group and has_budget and name)
    if complete and budget >= HOT_BUDGET_USD:
        return Score(
            "hot",
            "Destination, dates, group size, budget, and name are on file, "
            f"and the budget meets the ${HOT_BUDGET_USD} sample floor.",
        )
    if complete:
        return Score(
            "warm",
            "All qualifying fields are present, but the budget is under the "
            f"${HOT_BUDGET_USD} sample floor.",
        )
    return Score(
        "warm",
        "Destination is known; dates, group size, budget, or name is still missing.",
    )


def handoff_decision(score: Score, asked_for_human: bool) -> tuple[bool, str | None]:
    """Hand to a person when the lead is hot, or when they ask, at any score."""
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


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None
