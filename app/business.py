"""Trade and business configuration.

Edit `config/business.json` to reuse this bot for another trade: name, suburbs,
job types, hours, and the internal draft price bands. The model prompt lists
job types and suburbs. It does not include dollar amounts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_CACHE: dict[Path, tuple[int, "Business"]] = {}


@dataclass(frozen=True)
class QuoteBand:
    job_type: str
    label: str
    min_aud: int
    max_aud: int
    basis: str


@dataclass(frozen=True)
class JobType:
    id: str
    label: str
    aliases: tuple[str, ...]
    emergency: bool
    quote: QuoteBand


@dataclass(frozen=True)
class Ad:
    ad_id: str
    campaign: str | None
    source: str | None
    headline: str
    job_type: str | None


@dataclass(frozen=True)
class Business:
    name: str
    trade: str
    assistant_name: str
    area_name: str
    timezone: str
    currency: str
    slot_minutes: int
    booking_horizon_days: int
    banner: str
    suburbs: tuple[str, ...]
    hours: dict[int, tuple[time, time]]
    job_types: tuple[JobType, ...]
    ads: tuple[Ad, ...]
    source_tags: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def hours_for(self, day) -> tuple[time, time] | None:
        return self.hours.get(day.weekday())

    def job_label(self, job_type: str | None) -> str | None:
        found = self.job(job_type)
        if found:
            return found.label
        text = _text(job_type)
        return text

    def job(self, job_type: str | None) -> JobType | None:
        token = _text(job_type)
        if not token:
            return None
        folded = token.casefold()
        for job in self.job_types:
            if job.id.casefold() == folded or job.label.casefold() == folded:
                return job
            if any(alias.casefold() == folded for alias in job.aliases):
                return job
        return None

    def resolve_job_type(self, job_type: str | None) -> str | None:
        found = self.job(job_type)
        return found.id if found else None

    def quote_for(self, job_type: str | None) -> QuoteBand | None:
        found = self.job(job_type)
        return found.quote if found else None

    def canonical_suburb(self, suburb: str | None) -> str | None:
        text = _text(suburb)
        if not text:
            return None
        folded = text.casefold()
        for known in self.suburbs:
            if known.casefold() == folded:
                return known
        return text

    def in_area(self, suburb: str | None) -> bool:
        text = _text(suburb)
        if not text:
            return False
        folded = text.casefold()
        return any(known.casefold() == folded for known in self.suburbs)

    def find_ad(self, ad_id: str | None) -> Ad | None:
        if not ad_id:
            return None
        for ad in self.ads:
            if ad.ad_id == ad_id:
                return ad
        return None

    def prompt_block(self) -> str:
        """Job types and suburbs for the model. Price bands are intentionally absent."""
        lines = [
            f"Business: {self.name}, trade: {self.trade}.",
            f"Service area ({self.area_name}): {', '.join(self.suburbs)}.",
            "Do not mention prices, dollar amounts, or quote ranges.",
            "Job types:",
        ]
        for job in self.job_types:
            aliases = ", ".join(job.aliases)
            flag = " Treat clear fault language as emergency when they describe danger." if job.emergency else ""
            lines.append(f"- {job.id}: {job.label}. Customer words: {aliases}.{flag}")
        return "\n".join(lines)


def default_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "business.json"


def load_business(path: str | Path | None = None) -> Business:
    config_path = Path(path) if path else default_config_path()
    config_path = config_path.resolve()
    stamp = config_path.stat().st_mtime_ns
    cached = _CACHE.get(config_path)
    if cached and cached[0] == stamp:
        return cached[1]
    business = parse_business(json.loads(config_path.read_text(encoding="utf-8")))
    _CACHE[config_path] = (stamp, business)
    return business


def parse_business(data: dict) -> Business:
    if not isinstance(data, dict):
        raise ValueError("Business config must be a JSON object")
    name = _required(data, "business_name")
    trade = _required(data, "trade")
    assistant = _required(data, "assistant_name")
    timezone_name = _required(data, "timezone")
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise ValueError(f"Unknown timezone {timezone_name!r}") from exc

    suburbs = tuple(_required_list(data, "service_area_suburbs"))
    if not suburbs:
        raise ValueError("service_area_suburbs must list at least one suburb")

    job_types = tuple(_parse_job(item) for item in _required_list(data, "job_types"))
    if not job_types:
        raise ValueError("job_types must list at least one job")
    ids = [job.id for job in job_types]
    if len(ids) != len(set(ids)):
        raise ValueError("job type ids must be unique")

    ads = tuple(_parse_ad(item) for item in data.get("sample_ads") or [])
    ad_ids = [ad.ad_id for ad in ads]
    if len(ad_ids) != len(set(ad_ids)):
        raise ValueError("sample ad ids must be unique")

    slot_minutes = int(data.get("slot_minutes") or 90)
    horizon = int(data.get("booking_horizon_days") or 10)
    if slot_minutes < 15 or horizon < 1:
        raise ValueError("slot_minutes and booking_horizon_days are out of range")

    return Business(
        name=name,
        trade=trade,
        assistant_name=assistant,
        area_name=_text(data.get("area_name")) or name,
        timezone=timezone_name,
        currency=_text(data.get("currency")) or "AUD",
        slot_minutes=slot_minutes,
        booking_horizon_days=horizon,
        banner=_text(data.get("banner")) or "",
        suburbs=suburbs,
        hours=_parse_hours(data.get("hours") or {}),
        job_types=job_types,
        ads=ads,
        source_tags=_parse_source_tags(data),
    )


def _parse_job(item: dict) -> JobType:
    if not isinstance(item, dict):
        raise ValueError("job type must be an object")
    job_id = _required(item, "id")
    label = _required(item, "label")
    quote = item.get("quote") or {}
    if not isinstance(quote, dict):
        raise ValueError(f"job {job_id} quote must be an object")
    minimum = int(quote["min_aud"])
    maximum = int(quote["max_aud"])
    if minimum < 0 or maximum < minimum:
        raise ValueError(f"job {job_id} has an invalid price range")
    aliases = tuple(str(alias).strip() for alias in item.get("aliases") or [] if str(alias).strip())
    return JobType(
        id=job_id,
        label=label,
        aliases=aliases or (label,),
        emergency=bool(item.get("emergency")),
        quote=QuoteBand(
            job_type=job_id,
            label=label,
            min_aud=minimum,
            max_aud=maximum,
            basis=_text(quote.get("basis")) or "guide only; confirmed on site",
        ),
    )


def _parse_source_tags(data: dict) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Prefilled click-to-chat tags. Missing key keeps the `(web)` and `(qr)` defaults."""
    if "source_tags" not in data or data.get("source_tags") is None:
        raw = [
            {"source": "website", "tags": ["(web)", "[web]"]},
            {"source": "qr", "tags": ["(qr)", "[qr]"]},
        ]
    else:
        raw = data["source_tags"]
    if not isinstance(raw, list):
        raise ValueError("source_tags must be a list")
    allowed = {"ad", "organic", "website", "qr", "repeat"}
    parsed: list[tuple[str, tuple[str, ...]]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("source tag must be an object")
        source = _required(item, "source").lower()
        if source not in allowed:
            raise ValueError(f"Unknown job source {source}")
        tags = tuple(
            str(tag).strip() for tag in (item.get("tags") or []) if str(tag).strip()
        )
        if not tags:
            raise ValueError(f"source {source} needs at least one tag")
        parsed.append((source, tags))
    return tuple(parsed)


def _parse_ad(item: dict) -> Ad:
    if not isinstance(item, dict):
        raise ValueError("sample ad must be an object")
    return Ad(
        ad_id=_required(item, "ad_id"),
        campaign=_text(item.get("campaign")),
        source=_text(item.get("source")),
        headline=_required(item, "headline"),
        job_type=_text(item.get("job_type")),
    )


def _parse_hours(raw: dict) -> dict[int, tuple[time, time]]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("hours must map weekdays to [open, close]")
    hours: dict[int, tuple[time, time]] = {}
    for key, window in raw.items():
        day = _WEEKDAYS.get(str(key).strip().lower())
        if day is None:
            raise ValueError(f"Unknown weekday {key!r}")
        if not isinstance(window, list) or len(window) != 2:
            raise ValueError(f"Hours for {key} must be [open, close]")
        opening = _parse_time(window[0])
        closing = _parse_time(window[1])
        if closing <= opening:
            raise ValueError(f"Hours for {key} must close after they open")
        hours[day] = (opening, closing)
    return hours


def _parse_time(value: str) -> time:
    hour, minute = str(value).split(":")
    return time(int(hour), int(minute))


def _required(data: dict, key: str) -> str:
    text = _text(data.get(key))
    if not text:
        raise ValueError(f"Business config is missing {key}")
    return text


def _required_list(data: dict, key: str) -> list:
    value = data.get(key)
    if not isinstance(value, list):
        raise ValueError(f"Business config is missing {key}")
    return value


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
