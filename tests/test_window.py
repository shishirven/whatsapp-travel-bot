from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.service import deliver_customer
from app.window import (
    TEMPLATE_BOOKING_CONFIRMATION,
    TEMPLATE_QUOTE_FOLLOWUP,
    outbound_plan,
    window_open,
)
from tests.fakes import MemoryGateway

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def test_window_is_24_hours_inclusive():
    assert window_open(NOW - timedelta(hours=24), NOW)
    assert not window_open(NOW - timedelta(hours=24, seconds=1), NOW)
    assert not window_open(None, NOW)


def test_outside_the_window_picks_the_template_stubs():
    closed = NOW - timedelta(days=2)
    booking = outbound_plan(closed, NOW, "booking_confirmation")
    quote = outbound_plan(closed, NOW, "quote_followup")
    session = outbound_plan(closed, NOW, "session")
    assert booking == {"mode": "template", "template": TEMPLATE_BOOKING_CONFIRMATION}
    assert quote == {"mode": "template", "template": TEMPLATE_QUOTE_FOLLOWUP}
    assert session["mode"] == "held"
    assert outbound_plan(NOW, NOW, "quote_followup")["mode"] == "text"


def test_template_names_are_marked_as_unapproved_stubs():
    source = Path("app/window.py").read_text(encoding="utf-8")
    assert "TODO" in source
    assert TEMPLATE_BOOKING_CONFIRMATION == "booking_confirmation"
    assert TEMPLATE_QUOTE_FOLLOWUP == "quote_followup"


def test_deliver_customer_sends_text_or_the_matching_template():
    gateway = MemoryGateway()
    _, status, stored = deliver_customer(
        gateway,
        "61400000000",
        "You're booked for Mon 16 Nov, 10:30 am – 12:00 pm.",
        purpose="booking_confirmation",
        last_customer_at=NOW,
        now=NOW,
        template_params=["Sam", "Mon 16 Nov, 10:30 am"],
    )
    assert status == "sent"
    assert gateway.sent[-1][0] == "text"

    _, status, stored = deliver_customer(
        gateway,
        "61400000000",
        "You're booked for Mon 16 Nov, 10:30 am – 12:00 pm.",
        purpose="booking_confirmation",
        last_customer_at=NOW - timedelta(days=3),
        now=NOW,
        template_params=["Sam", "Mon 16 Nov, 10:30 am"],
    )
    assert status == "sent"
    assert gateway.sent[-1][0] == "template"
    assert gateway.sent[-1][2] == "booking_confirmation"
    assert stored.startswith("[template:booking_confirmation]")
    assert gateway.sent[-1][1] == "61400000000"
