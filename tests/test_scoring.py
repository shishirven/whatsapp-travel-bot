import pytest

from app.business import load_business, parse_business
from app.scoring import Score, canonical_facts, handoff_decision, merge_facts, score_lead, wants_human

BUSINESS = load_business()

FULL = {
    "job_type": "power_point",
    "suburb": "Newtown",
    "urgency": "planned",
    "description": "Two extra points in the lounge for the TV.",
    "preferred_time": None,
    "contact_name": "Jamie Lee",
}


@pytest.mark.parametrize(
    ("facts", "label"),
    [
        ({}, "cold"),
        ({"contact_name": "Sam"}, "cold"),
        ({"job_type": "power_point"}, "warm"),
        ({"job_type": "fault", "suburb": "Newtown"}, "warm"),
        ({**FULL, "suburb": "Bondi"}, "warm"),
        (FULL, "hot"),
        (
            {"job_type": "fault", "suburb": "Newtown", "urgency": "emergency"},
            "hot",
        ),
    ],
)
def test_score_labels(facts, label):
    assert score_lead(facts, BUSINESS).label == label


def test_emergency_in_area_is_hot_before_the_checklist_is_full():
    reason = score_lead(
        {"job_type": "safety switch", "suburb": "newtown", "urgency": "emergency"},
        BUSINESS,
    ).reason
    assert "Emergency" in reason
    assert "service area" in reason


def test_outside_the_service_area_stays_warm():
    score = score_lead({**FULL, "suburb": "Bondi"}, BUSINESS)
    assert score.label == "warm"
    assert "outside" in score.reason


def test_merge_keeps_previous_facts_and_overwrites_new_ones():
    merged = merge_facts(
        {
            "job_type": "fault",
            "suburb": None,
            "urgency": None,
            "description": None,
            "preferred_time": None,
            "contact_name": None,
        },
        {
            "job_type": None,
            "suburb": "Newtown",
            "urgency": None,
            "description": "",
            "preferred_time": None,
            "contact_name": "Sam",
        },
    )
    assert merged["job_type"] == "fault"
    assert merged["suburb"] == "Newtown"
    assert merged["contact_name"] == "Sam"
    assert merged["description"] is None


def test_canonical_facts_use_config_ids():
    facts = canonical_facts(
        {"job_type": "safety switch", "suburb": "newtown", "urgency": "ASAP"},
        BUSINESS,
    )
    assert facts["job_type"] == "fault"
    assert facts["suburb"] == "Newtown"
    assert facts["urgency"] == "urgent"


def test_wants_human_matches_phrases_not_loose_words():
    assert wants_human("Can I talk to a person?")
    assert wants_human("please call me tomorrow")
    assert wants_human("Can I speak to the electrician?")
    assert not wants_human("I'm travelling with someone in my family")
    assert not wants_human("")
    assert not wants_human(None)


def test_handoff_when_hot_or_when_asked():
    hot = Score("hot", "ready")
    warm = Score("warm", "partial")
    assert handoff_decision(hot, False) == (True, "hot_lead")
    assert handoff_decision(hot, True) == (True, "hot_lead")
    assert handoff_decision(warm, True) == (True, "human_requested")
    assert handoff_decision(warm, False) == (False, None)


def test_another_trade_uses_its_own_suburbs_and_job_types():
    plumber = parse_business(
        {
            "business_name": "Bondi Plumbing",
            "trade": "plumber",
            "assistant_name": "Pat",
            "timezone": "Australia/Sydney",
            "service_area_suburbs": ["Bondi", "Coogee"],
            "hours": {"mon": ["08:00", "16:00"]},
            "job_types": [
                {
                    "id": "tap",
                    "label": "Tap washer",
                    "aliases": ["dripping tap", "leaking tap"],
                    "quote": {"min_aud": 120, "max_aud": 250, "basis": "washer only"},
                }
            ],
        }
    )
    assert score_lead({**FULL, "job_type": "tap", "suburb": "Bondi"}, plumber).label == "hot"
    assert score_lead(FULL, plumber).label == "warm"
    assert plumber.quote_for("dripping tap").min_aud == 120
