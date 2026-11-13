"""Offer three free visit slots and book the one the customer picks.

`Calendar` is the seam. `DatabaseCalendar` is the local stand-in used when
Google credentials are absent. `GoogleCalendar` talks to the Calendar API with
a service account or an OAuth refresh token. Tests pass a fake.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from app.business import Business

logger = logging.getLogger("tradie.booking")

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_ORDINALS = {
    "1": 0,
    "first": 0,
    "2": 1,
    "second": 1,
    "3": 2,
    "third": 2,
}
_PICK = re.compile(
    r"^(?:option\s+|slot\s+|#)?(1|2|3|first|second|third)(?:\s+one)?$",
    re.IGNORECASE,
)
_TAKE = re.compile(
    r"\b(?:take|book|choose|pick)\s+(?:the\s+)?(?:option\s+|slot\s+)?(1|2|3|first|second|third)\b",
    re.IGNORECASE,
)


class CalendarError(RuntimeError):
    """The calendar accepted the call shape but did not book."""


class Calendar(Protocol):
    def busy_intervals(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        """Return busy ranges that overlap [start, end)."""

    def create_event(
        self,
        start: datetime,
        end: datetime,
        summary: str,
        description: str,
    ) -> str:
        """Create one event. Return the provider event id."""


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime
    label: str

    def as_dict(self) -> dict:
        return {
            "start": self.start.isoformat(timespec="seconds"),
            "end": self.end.isoformat(timespec="seconds"),
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Slot":
        return cls(
            start=_parse_iso(data["start"]),
            end=_parse_iso(data["end"]),
            label=data["label"],
        )


class DatabaseCalendar:
    """Busy time is the visits this app has already stored. No Google account."""

    def __init__(self, db) -> None:
        self.db = db

    def busy_intervals(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        found = []
        for begin, finish in self.db.booked_intervals():
            if begin < end and finish > start:
                found.append((begin, finish))
        return found

    def create_event(self, start: datetime, end: datetime, summary: str, description: str) -> str:
        return f"local-{uuid.uuid4().hex[:12]}"


class GoogleCalendar:
    """Google Calendar via a bearer token.

    `token_provider` returns an access token. `transport(method, url, headers, json)`
    performs the HTTP call and returns the decoded JSON body. Neither is used
    until a slot is listed or booked, so tests can pass fakes.
    """

    def __init__(self, token_provider, calendar_id: str, transport) -> None:
        self.token_provider = token_provider
        self.calendar_id = calendar_id
        self.transport = transport

    def busy_intervals(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        payload = {
            "timeMin": _utc_iso(start),
            "timeMax": _utc_iso(end),
            "items": [{"id": self.calendar_id}],
        }
        data = self._request("POST", "https://www.googleapis.com/calendar/v3/freeBusy", payload)
        calendar = (data.get("calendars") or {}).get(self.calendar_id) or {}
        intervals = []
        for item in calendar.get("busy") or []:
            intervals.append((_parse_iso(item["start"]), _parse_iso(item["end"])))
        return intervals

    def create_event(self, start: datetime, end: datetime, summary: str, description: str) -> str:
        payload = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat(timespec="seconds")},
            "end": {"dateTime": end.isoformat(timespec="seconds")},
        }
        url = (
            "https://www.googleapis.com/calendar/v3/calendars/"
            f"{self.calendar_id}/events"
        )
        data = self._request("POST", url, payload)
        event_id = data.get("id")
        if not event_id:
            raise CalendarError(f"calendar response had no event id: {data!r}")
        return str(event_id)

    def _request(self, method: str, url: str, payload: dict) -> dict:
        token = self.token_provider()
        try:
            return self.transport(
                method,
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
        except CalendarError:
            raise
        except Exception as exc:
            raise CalendarError(str(exc)) from exc


def service_account_token_provider(info: dict):
    """Return a callable that mints a Calendar access token from a service account."""

    def provider() -> str:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/calendar"],
        )
        credentials.refresh(Request())
        if not credentials.token:
            raise CalendarError("service account refresh returned no token")
        return credentials.token

    return provider


def oauth_refresh_body(client_id: str, client_secret: str, refresh_token: str) -> dict:
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }


def oauth_token_provider(client_id: str, client_secret: str, refresh_token: str, post):
    """`post(url, data)` returns the token JSON. Tests pass a fake."""

    def provider() -> str:
        data = post(
            "https://oauth2.googleapis.com/token",
            oauth_refresh_body(client_id, client_secret, refresh_token),
        )
        token = (data or {}).get("access_token")
        if not token:
            raise CalendarError("OAuth refresh returned no access token")
        return token

    return provider


def httpx_transport(method: str, url: str, *, headers: dict, json: dict | None = None) -> dict:
    import httpx

    response = httpx.request(method, url, headers=headers, json=json, timeout=20)
    response.raise_for_status()
    return response.json()


def build_calendar(settings, db) -> Calendar:
    """Service account, then OAuth, then the local database calendar."""
    info = _service_account_info(settings)
    if info is not None:
        if not settings.google_calendar_id:
            raise ValueError("GOOGLE_CALENDAR_ID is required when Google credentials are set")
        logger.info("calendar=google service_account id=%s", settings.google_calendar_id)
        return GoogleCalendar(
            token_provider=service_account_token_provider(info),
            calendar_id=settings.google_calendar_id,
            transport=httpx_transport,
        )
    if (
        settings.google_oauth_client_id
        and settings.google_oauth_client_secret
        and settings.google_oauth_refresh_token
    ):
        if not settings.google_calendar_id:
            raise ValueError("GOOGLE_CALENDAR_ID is required when Google OAuth is set")

        def post(url, data):
            import httpx

            response = httpx.post(url, data=data, timeout=20)
            response.raise_for_status()
            return response.json()

        logger.info("calendar=google oauth id=%s", settings.google_calendar_id)
        return GoogleCalendar(
            token_provider=oauth_token_provider(
                settings.google_oauth_client_id,
                settings.google_oauth_client_secret,
                settings.google_oauth_refresh_token,
                post,
            ),
            calendar_id=settings.google_calendar_id,
            transport=httpx_transport,
        )
    logger.info("calendar=local")
    return DatabaseCalendar(db)


def available_slots(
    business: Business,
    calendar: Calendar,
    now: datetime,
    count: int = 3,
) -> list[Slot]:
    """The next `count` free slots inside configured business hours."""
    zone = business.zone()
    now_local = now.astimezone(zone)
    horizon = now_local + timedelta(days=business.booking_horizon_days)
    try:
        busy = calendar.busy_intervals(now_local, horizon)
    except Exception:
        logger.exception("calendar busy lookup failed")
        busy = []
    duration = timedelta(minutes=business.slot_minutes)
    found: list[Slot] = []
    day = now_local.date()
    last_day = horizon.date()
    while day <= last_day and len(found) < count:
        window = business.hours_for(day)
        if window:
            opening, closing = window
            cursor = datetime.combine(day, opening, tzinfo=zone)
            close_at = datetime.combine(day, closing, tzinfo=zone)
            while cursor + duration <= close_at and len(found) < count:
                end = cursor + duration
                if cursor > now_local and not _overlaps(cursor, end, busy):
                    found.append(Slot(start=cursor, end=end, label=format_slot(cursor, end)))
                cursor = end
        day += timedelta(days=1)
    return found


def format_slot(start: datetime, end: datetime) -> str:
    return (
        f"{_WEEKDAYS[start.weekday()]} {start.day} {_MONTHS[start.month]}, "
        f"{_format_time(start)} – {_format_time(end)}"
    )


def format_offer(slots: list[Slot], preferred: str | None = None) -> str:
    lines = []
    if preferred:
        lines.append(f"You mentioned {preferred}.")
    if not slots:
        lines.append("I couldn't see a free visit in the next stretch of business hours. A person will propose a time.")
        return " ".join(lines)
    lines.append("Next free visits — reply 1, 2, or 3:")
    lines.extend(f"{index}. {slot.label}" for index, slot in enumerate(slots, start=1))
    return "\n".join(lines)


def format_confirmation(slot: Slot) -> str:
    return (
        f"You're booked for {slot.label}. "
        "The electrician will message if anything changes."
    )


def match_slot(text: str | None, slots: list[Slot]) -> Slot | None:
    if not text or not slots:
        return None
    cleaned = " ".join(text.strip().split())
    cleaned = re.sub(r"\b(please|thanks|thank you)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = " ".join(cleaned.split()).strip(" .!")
    direct = _PICK.match(cleaned)
    if direct:
        index = _ORDINALS[direct.group(1).lower()]
        return slots[index] if index < len(slots) else None
    taken = _TAKE.search(cleaned)
    if taken:
        index = _ORDINALS[taken.group(1).lower()]
        return slots[index] if index < len(slots) else None
    lowered = cleaned.casefold()
    hits = []
    for slot in slots:
        token = _format_time(slot.start).casefold()
        clock = slot.start.strftime("%H:%M")
        if token in lowered or clock in lowered:
            hits.append(slot)
    if len(hits) == 1:
        return hits[0]
    return None


def booking_from_slots(slots: list[Slot]) -> dict:
    return {
        "status": "offered",
        "slots": [slot.as_dict() for slot in slots],
        "event_id": None,
        "start": None,
        "end": None,
        "label": None,
    }


def offered_slots(booking: dict | None, now: datetime) -> list[Slot]:
    if not booking or booking.get("status") != "offered":
        return []
    slots = [Slot.from_dict(item) for item in booking.get("slots") or []]
    zone = now.tzinfo
    open_slots = []
    for slot in slots:
        start = slot.start if zone is None else slot.start.astimezone(zone)
        current = now if slot.start.tzinfo is None else now.astimezone(slot.start.tzinfo)
        if start > current:
            open_slots.append(slot)
    return open_slots


def mark_booked(booking: dict, slot: Slot, event_id: str) -> dict:
    updated = dict(booking)
    updated.update(
        {
            "status": "booked",
            "event_id": event_id,
            "start": slot.start.isoformat(timespec="seconds"),
            "end": slot.end.isoformat(timespec="seconds"),
            "label": slot.label,
        }
    )
    return updated


def _overlaps(start: datetime, end: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
    for begin, finish in busy:
        begin = begin.astimezone(start.tzinfo) if begin.tzinfo and start.tzinfo else begin
        finish = finish.astimezone(start.tzinfo) if finish.tzinfo and start.tzinfo else finish
        if start < finish and end > begin:
            return True
    return False


def _format_time(value: datetime) -> str:
    hour = value.hour % 12 or 12
    suffix = "am" if value.hour < 12 else "pm"
    return f"{hour}:{value.minute:02d} {suffix}"


def _parse_iso(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _service_account_info(settings) -> dict | None:
    raw = (settings.google_service_account_json or "").strip()
    if raw:
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON") from exc
        if not isinstance(info, dict):
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON must be an object")
        return info
    path = (settings.google_credentials_file or "").strip()
    if not path:
        return None
    with open(path, encoding="utf-8") as handle:
        info = json.load(handle)
    if not isinstance(info, dict):
        raise ValueError("Google credentials file must contain a JSON object")
    return info
