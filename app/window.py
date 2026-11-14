"""WhatsApp 24-hour customer-care window.

Free-form session text is allowed only within 24 hours of the customer's last
message. Outside that window the send must be a template. The names below are
stubs: WhatsApp Manager has to approve the real templates before they deliver.

TODO: Replace these stubs with the template names approved in WhatsApp Manager.
`booking_confirmation` and `quote_followup` are placeholders, not approved names.
Meta rejects the send until those templates exist on the WABA, or these
constants are changed to the approved names.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# TODO: replace with the approved WhatsApp template name for a locked-in visit.
TEMPLATE_BOOKING_CONFIRMATION = "booking_confirmation"
# TODO: replace with the approved WhatsApp template name for a quote follow-up.
TEMPLATE_QUOTE_FOLLOWUP = "quote_followup"

_WINDOW = timedelta(hours=24)


def window_open(last_customer_at: datetime | None, now: datetime) -> bool:
    """True when a free-form reply is still inside the 24-hour care window."""
    if last_customer_at is None:
        return False
    last_customer_at = _aware(last_customer_at)
    now = _aware(now)
    return now - last_customer_at <= _WINDOW


def outbound_plan(last_customer_at: datetime | None, now: datetime, purpose: str) -> dict:
    """Choose free-form text or a template stub for one outbound message.

    `purpose` is `session` (a reply in the live chat), `booking_confirmation`,
    or `quote_followup`. Session replies have no template of their own: outside
    the window they are held rather than sent as free text.
    """
    if window_open(last_customer_at, now):
        return {"mode": "text", "template": None}
    if purpose == "booking_confirmation":
        return {"mode": "template", "template": TEMPLATE_BOOKING_CONFIRMATION}
    if purpose == "quote_followup":
        return {"mode": "template", "template": TEMPLATE_QUOTE_FOLLOWUP}
    return {"mode": "held", "template": None}


def parse_stored_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith(" UTC"):
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S UTC")
        return parsed.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(text)
    return _aware(parsed)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
