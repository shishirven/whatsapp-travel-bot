from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.booking import (
    GoogleCalendar,
    available_slots,
    booking_from_slots,
    build_calendar,
    format_slot,
    match_slot,
    oauth_refresh_body,
)
from app.service import _maybe_book
from app.business import load_business
from tests.conftest import make_settings
from tests.fakes import FakeCalendar

SYDNEY = ZoneInfo("Australia/Sydney")
# Monday 16 Nov 2026, 9:00am AEDT. Slots are 90 minutes from 7:30.
NOW = datetime(2026, 11, 16, 9, 0, tzinfo=SYDNEY)


def test_three_slots_fall_inside_business_hours():
    slots = available_slots(load_business(), FakeCalendar(), NOW, count=3)
    assert len(slots) == 3
    assert [slot.label for slot in slots] == [
        "Mon 16 Nov, 10:30 am – 12:00 pm",
        "Mon 16 Nov, 12:00 pm – 1:30 pm",
        "Mon 16 Nov, 1:30 pm – 3:00 pm",
    ]
    for slot in slots:
        assert slot.start.weekday() < 5
        assert slot.start.hour >= 7
        assert slot.end.hour < 16 or (slot.end.hour == 16 and slot.end.minute == 0)
        assert slot.end - slot.start == timedelta(minutes=90)


def test_busy_time_is_skipped():
    busy_start = datetime(2026, 11, 16, 10, 30, tzinfo=SYDNEY)
    busy = [(busy_start, busy_start + timedelta(minutes=90))]
    slots = available_slots(load_business(), FakeCalendar(busy), NOW, count=3)
    labels = [slot.label for slot in slots]
    assert "10:30 am" not in labels[0]
    assert labels[0] == "Mon 16 Nov, 12:00 pm – 1:30 pm"
    assert labels[2].startswith("Tue 17 Nov")


def test_weekend_is_not_offered():
    saturday = datetime(2026, 11, 14, 10, 0, tzinfo=SYDNEY)
    slots = available_slots(load_business(), FakeCalendar(), saturday, count=1)
    assert slots[0].start.weekday() == 0
    assert slots[0].label.startswith("Mon 16 Nov, 7:30 am")


def test_match_slot_is_strict_about_numbers():
    slots = available_slots(load_business(), FakeCalendar(), NOW, count=3)
    assert match_slot("1", slots) == slots[0]
    assert match_slot("option 2 please", slots) == slots[1]
    assert match_slot("I'll take the third", slots) == slots[2]
    assert match_slot("2 extra points in the lounge", slots) is None
    assert match_slot("10:30 am works", slots) == slots[0]
    assert match_slot("", slots) is None


def test_google_calendar_builds_freebusy_and_event_requests():
    calls = []

    def transport(method, url, *, headers, json):
        calls.append((method, url, headers, json))
        if url.endswith("/freeBusy"):
            return {
                "calendars": {
                    "cal": {
                        "busy": [
                            {"start": "2026-11-16T00:00:00Z", "end": "2026-11-16T01:00:00Z"}
                        ]
                    }
                }
            }
        return {"id": "evt-9"}

    calendar = GoogleCalendar(lambda: "token", "cal", transport)
    start = datetime(2026, 11, 16, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=2)
    busy = calendar.busy_intervals(start, end)
    assert busy[0][0] == start
    assert calendar.create_event(start, end, "Switchboard in Newtown", "WhatsApp: 614") == "evt-9"
    assert calls[0][0] == "POST"
    assert calls[0][2]["Authorization"] == "Bearer token"
    assert calls[0][3]["items"] == [{"id": "cal"}]
    assert calls[1][1].endswith("/calendars/cal/events")
    assert calls[1][3]["summary"] == "Switchboard in Newtown"


def test_oauth_refresh_body_and_invalid_service_account(tmp_path):
    body = oauth_refresh_body("id", "secret", "refresh")
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "refresh"
    settings = make_settings(
        tmp_path,
        google_service_account_json="{",
        google_calendar_id="cal",
    )
    with pytest.raises(ValueError):
        build_calendar(settings, db=None)


def test_a_slot_taken_since_the_offer_is_not_booked_again():
    business = load_business()
    slots = available_slots(business, FakeCalendar(), NOW, count=3)
    booking = booking_from_slots(slots)
    taken = FakeCalendar(busy=[(slots[0].start, slots[0].end)])
    reply, updated = _maybe_book(
        "1",
        booking,
        business,
        taken,
        {"job_type": "fault", "suburb": "Newtown", "contact_name": "Sam", "urgency": "planned", "description": "oven"},
        {"wa_id": "614000", "profile_name": "Sam"},
        NOW + timedelta(minutes=1),
    )
    assert taken.events == []
    assert "just taken" in reply.lower()
    assert updated["slots"][0]["label"] != slots[0].label


def test_format_slot_does_not_depend_on_locale():
    start = datetime(2026, 11, 16, 13, 30, tzinfo=SYDNEY)
    assert format_slot(start, start + timedelta(minutes=90)) == "Mon 16 Nov, 1:30 pm – 3:00 pm"
