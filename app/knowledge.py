"""Fictional tour packages and the Meta ads that point at them.

Every record is invented for this demo. The banner is included in the model
prompt and on the web pages so a reader cannot mistake it for a live tariff.
"""

from __future__ import annotations

SAMPLE_BANNER = (
    "SAMPLE DATA — fictional packages for this demo. Horizon Sample Travel is "
    "not a real agency. Nothing here is bookable, and the prices are made up."
)

SAMPLE_PACKAGES: list[dict] = [
    {
        "id": "bali-highlights",
        "name": "Bali Highlights",
        "destination": "Bali, Indonesia",
        "duration_days": 7,
        "price_from_usd": 1299,
        "summary": "Rice terraces, a temple sunset, and a day on Nusa Penida.",
        "highlights": ["Ubud rice terraces", "Uluwatu sunset", "Nusa Penida day trip"],
        "fictional": True,
    },
    {
        "id": "kenya-safari",
        "name": "Kenya Safari Week",
        "destination": "Kenya",
        "duration_days": 8,
        "price_from_usd": 2450,
        "summary": "Two safari camps and a conservancy game drive, with a night in Nairobi.",
        "highlights": ["Amboseli lookout", "Conservancy game drive", "Nairobi overnight"],
        "fictional": True,
    },
    {
        "id": "japan-rail",
        "name": "Japan by Rail",
        "destination": "Japan",
        "duration_days": 7,
        "price_from_usd": 3100,
        "summary": "Tokyo, a Kyoto stay, and rail between them on a sample week.",
        "highlights": ["Tokyo neighborhoods", "Kyoto temples", "Rail between the two cities"],
        "fictional": True,
    },
    {
        "id": "morocco-medina",
        "name": "Marrakech Medina",
        "destination": "Morocco",
        "duration_days": 6,
        "price_from_usd": 1590,
        "summary": "The medina, a day in the Atlas foothills, and a cooking afternoon.",
        "highlights": ["Marrakech medina", "Atlas foothills day", "Cooking afternoon"],
        "fictional": True,
    },
]

# Click-to-WhatsApp ads. `ad_id` is what Meta would send as referral.source_id.
SAMPLE_ADS: list[dict] = [
    {
        "ad_id": "ad_bali_summer_01",
        "campaign": "meta_bali_summer_2026",
        "source": "facebook",
        "headline": "7 days in Bali from $1,299",
        "package_id": "bali-highlights",
    },
    {
        "ad_id": "ad_kenya_safari_02",
        "campaign": "meta_kenya_safari_2026",
        "source": "instagram",
        "headline": "Kenya safari week from $2,450",
        "package_id": "kenya-safari",
    },
    {
        "ad_id": "ad_japan_rail_03",
        "campaign": "meta_japan_spring_2026",
        "source": "facebook",
        "headline": "Japan by rail from $3,100",
        "package_id": "japan-rail",
    },
    {
        "ad_id": "ad_morocco_04",
        "campaign": "meta_morocco_2026",
        "source": "instagram",
        "headline": "Marrakech in 6 days from $1,590",
        "package_id": "morocco-medina",
    },
]

# Longer aliases first so "nusa penida" wins over a shorter token at the same spot.
_ALIASES: tuple[tuple[str, str], ...] = (
    ("nusa penida", "Bali, Indonesia"),
    ("ubud", "Bali, Indonesia"),
    ("bali", "Bali, Indonesia"),
    ("maasai", "Kenya"),
    ("masai", "Kenya"),
    ("safari", "Kenya"),
    ("kenya", "Kenya"),
    ("kyoto", "Japan"),
    ("tokyo", "Japan"),
    ("japan", "Japan"),
    ("marrakesh", "Morocco"),
    ("marrakech", "Morocco"),
    ("morocco", "Morocco"),
)


def find_ad(ad_id: str | None) -> dict | None:
    if not ad_id:
        return None
    for ad in SAMPLE_ADS:
        if ad["ad_id"] == ad_id:
            return ad
    return None


def find_package(package_id: str) -> dict | None:
    for package in SAMPLE_PACKAGES:
        if package["id"] == package_id:
            return package
    return None


def package_for_destination(destination: str | None) -> dict | None:
    if not destination:
        return None
    for package in SAMPLE_PACKAGES:
        if package["destination"] == destination:
            return package
    return None


def destination_aliases() -> tuple[tuple[str, str], ...]:
    return _ALIASES


def prompt_block() -> str:
    """Plain-text tariff for the system prompt. Kept short on purpose."""
    lines = [SAMPLE_BANNER, ""]
    for package in SAMPLE_PACKAGES:
        highlights = ", ".join(package["highlights"])
        lines.append(
            f"- [SAMPLE] {package['name']} ({package['destination']}), "
            f"{package['duration_days']} days, from ${package['price_from_usd']} USD. "
            f"{package['summary']} Highlights: {highlights}."
        )
    return "\n".join(lines)
