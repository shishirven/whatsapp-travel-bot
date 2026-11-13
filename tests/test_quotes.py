from app.business import load_business
from app.quotes import customer_quote_text, draft_quote, scrub_prices


def test_draft_comes_from_config_and_a_sent_quote_is_kept():
    business = load_business()
    draft = draft_quote(business, "switchboard", None)
    assert draft["status"] == "draft"
    assert draft["min_aud"] == 1800
    assert draft["max_aud"] == 4500
    sent = dict(draft, status="sent", sent_via="text")
    assert draft_quote(business, "fault", sent)["status"] == "sent"
    assert draft_quote(business, "not-a-job", None) is None


def test_customer_text_is_a_range_the_bot_must_not_invent():
    business = load_business()
    quote = draft_quote(business, "fault", None)
    text = customer_quote_text(business, quote, contact_name="Jamie Lee", suburb="Newtown")
    assert "$220–$480 AUD" in text
    assert "not a fixed quote" in text
    assert "Jamie Lee" in text


def test_scrub_prices_drops_only_the_money_sentence():
    cleaned = scrub_prices("A switchboard is $1,800 to $4,500. Which suburb?")
    assert cleaned == "Which suburb?"
    assert scrub_prices("Which suburb?") == "Which suburb?"
    assert "$" not in scrub_prices("The range is 1800 dollars.")
