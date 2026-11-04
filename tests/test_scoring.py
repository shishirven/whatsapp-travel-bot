import pytest

from app.scoring import HOT_BUDGET_USD, Score, handoff_decision, merge_facts, score_lead, wants_human

FULL = {
    "destination": "Bali, Indonesia",
    "travel_dates": "June 12-19 2026",
    "group_size": 4,
    "budget_usd": 4000,
    "contact_name": "Jamie Lee",
}


@pytest.mark.parametrize(
    ("facts", "label"),
    [
        ({}, "cold"),
        ({"contact_name": "Sam"}, "cold"),
        ({"destination": "Bali, Indonesia"}, "warm"),
        ({"destination": "Kenya", "travel_dates": "next month"}, "warm"),
        ({**FULL, "budget_usd": 400}, "warm"),
        (FULL, "hot"),
    ],
)
def test_score_labels(facts, label):
    assert score_lead(facts).label == label


def test_hot_reason_names_the_floor():
    reason = score_lead(FULL).reason
    assert str(HOT_BUDGET_USD) in reason


def test_under_floor_is_warm_even_when_complete():
    score = score_lead({**FULL, "budget_usd": HOT_BUDGET_USD - 1})
    assert score.label == "warm"
    assert "under" in score.reason


def test_merge_keeps_previous_facts_and_overwrites_new_ones():
    merged = merge_facts(
        {"destination": "Bali, Indonesia", "travel_dates": None, "group_size": None, "budget_usd": None, "contact_name": None},
        {"destination": None, "travel_dates": "June", "group_size": None, "budget_usd": "", "contact_name": "Sam"},
    )
    assert merged["destination"] == "Bali, Indonesia"
    assert merged["travel_dates"] == "June"
    assert merged["contact_name"] == "Sam"
    assert merged["budget_usd"] is None


def test_wants_human_matches_phrases_not_loose_words():
    assert wants_human("Can I talk to a person?")
    assert wants_human("please call me tomorrow")
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
