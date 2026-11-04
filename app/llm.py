"""Qualifiers: Claude via tool use, and a keyword stand-in for offline demos.

Claude Sonnet 5.5 rejects forced tool choice (`tool_choice` any/tool). The
call uses `auto` plus a strict schema, and the prompt tells Maya to call the
tool every turn. If the model answers in plain text anyway, that text is the
reply and no new facts are stored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import anthropic

from app.extract import extract_facts
from app.knowledge import package_for_destination
from app.scoring import merge_facts, wants_human

logger = logging.getLogger("horizon.llm")

QUALIFY_TOOL = {
    "name": "record_lead_qualification",
    "description": (
        "Record what the traveller has actually said, and the exact WhatsApp "
        "reply to send. Call this on every turn. Use null for any fact they "
        "have not stated. Do not invent dates, group size, budget, or a name."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "reply": {
                "type": "string",
                "description": (
                    "Customer-facing WhatsApp message. One or two short sentences. "
                    "Ask at most one question."
                ),
            },
            "request_human": {
                "type": "boolean",
                "description": "True only when the traveller asks to speak with a person.",
            },
            "destination": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "Where they want to go, or null. The ad is not confirmation.",
            },
            "travel_dates": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "Dates or a rough month, in their words, or null.",
            },
            "group_size": {
                "anyOf": [{"type": "integer"}, {"type": "null"}],
                "description": "Number of travellers, or null.",
            },
            "budget_usd": {
                "anyOf": [{"type": "number"}, {"type": "null"}],
                "description": "Approximate budget converted to USD, or null.",
            },
            "contact_name": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "Name for the enquiry, or null. Do not copy the WhatsApp profile.",
            },
        },
        "required": [
            "reply",
            "request_human",
            "destination",
            "travel_dates",
            "group_size",
            "budget_usd",
            "contact_name",
        ],
    },
}

_FALLBACK_REPLY = "Could you tell me a little more about the trip you have in mind?"
_MAX_HISTORY = 20

_QUESTIONS = (
    ("destination", "Where are you hoping to go?"),
    ("travel_dates", "What dates, or even a rough month, are you looking at?"),
    ("group_size", "How many people are travelling?"),
    ("budget_usd", "What budget do you have in mind, in USD if that's easy?"),
    ("contact_name", "What name should I put on the enquiry?"),
)


@dataclass
class QualificationDraft:
    """One turn: the WhatsApp reply, plus any facts stated in that turn."""

    reply: str
    request_human: bool = False
    destination: str | None = None
    travel_dates: str | None = None
    group_size: int | None = None
    budget_usd: float | None = None
    contact_name: str | None = None
    raw: dict | None = None

    def facts(self) -> dict:
        return {
            "destination": self.destination,
            "travel_dates": self.travel_dates,
            "group_size": self.group_size,
            "budget_usd": self.budget_usd,
            "contact_name": self.contact_name,
        }


@dataclass
class TurnContext:
    """What a qualifier may look at for a single traveller message."""

    history: list[dict]
    known: dict
    ad: dict | None
    profile_name: str | None
    system: str


class AnthropicQualifier:
    def __init__(self, api_key: str, model: str, client=None) -> None:
        self.model = model
        self.client = client if client is not None else anthropic.Anthropic(api_key=api_key)

    def qualify(self, turn: TurnContext) -> QualificationDraft:
        # between_tools skips the long up-front thinking pass. Sonnet 5.5
        # rejects thinking type "disabled", and it also rejects forced tool_choice.
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1500,
            system=turn.system,
            tools=[QUALIFY_TOOL],
            tool_choice={"type": "auto"},
            thinking={"type": "between_tools"},
            messages=to_api_messages(turn.history[-_MAX_HISTORY:]),
        )
        return parse_qualification(response)


class RuleBasedQualifier:
    """Offline practice qualifier. Keyword extraction plus a fixed question order."""

    def qualify(self, turn: TurnContext) -> QualificationDraft:
        latest = _latest_user_text(turn.history)
        extracted = extract_facts(latest)
        merged = merge_facts(turn.known, extracted)
        asked = wants_human(latest)
        reply = _compose_reply(
            latest=latest,
            extracted=extracted,
            known=turn.known,
            merged=merged,
            ad=turn.ad,
            profile_name=turn.profile_name,
            history=turn.history,
            asked=asked,
        )
        raw = {"reply": reply, "request_human": asked, **extracted}
        return QualificationDraft(
            reply=reply,
            request_human=asked,
            destination=extracted["destination"],
            travel_dates=extracted["travel_dates"],
            group_size=extracted["group_size"],
            budget_usd=extracted["budget_usd"],
            contact_name=extracted["contact_name"],
            raw=raw,
        )


class MissingKeyQualifier:
    """Used when LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is empty."""

    def qualify(self, turn: TurnContext) -> QualificationDraft:
        return QualificationDraft(
            reply=(
                "Claude is selected (LLM_PROVIDER=anthropic) but ANTHROPIC_API_KEY is empty. "
                "Add the key to your .env file, or unset LLM_PROVIDER to use the offline qualifier."
            ),
            raw={"unconfigured": True},
        )


def parse_qualification(response) -> QualificationDraft:
    tool_input = None
    texts: list[str] = []
    for block in getattr(response, "content", None) or []:
        block_type = block["type"] if isinstance(block, dict) else getattr(block, "type", None)
        if block_type == "tool_use":
            name = block["name"] if isinstance(block, dict) else getattr(block, "name", None)
            if name == QUALIFY_TOOL["name"]:
                tool_input = block["input"] if isinstance(block, dict) else getattr(block, "input", None)
        elif block_type == "text":
            text = block["text"] if isinstance(block, dict) else getattr(block, "text", "")
            if text:
                texts.append(text)
    if isinstance(tool_input, dict):
        return _draft_from_tool(tool_input, texts)
    reply = " ".join(piece.strip() for piece in texts if piece and piece.strip())
    if not reply:
        logger.warning("model returned no tool call and no text")
        reply = _FALLBACK_REPLY
    return QualificationDraft(reply=reply, raw={"unstructured": True})


def to_api_messages(history: list[dict]) -> list[dict]:
    """Anthropic requires a user message first and alternating roles."""
    cleaned: list[dict] = []
    for item in history:
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        if cleaned and cleaned[-1]["role"] == role:
            cleaned[-1]["content"] += "\n" + content
        else:
            cleaned.append({"role": role, "content": content})
    if cleaned and cleaned[0]["role"] == "assistant":
        cleaned.insert(
            0,
            {
                "role": "user",
                "content": "(The traveller opened the chat. Your greeting was already sent.)",
            },
        )
    if not cleaned:
        cleaned = [{"role": "user", "content": "(no message)"}]
    return cleaned


def _draft_from_tool(data: dict, texts: list[str]) -> QualificationDraft:
    reply = _clean_str(data.get("reply"))
    if not reply:
        reply = " ".join(piece.strip() for piece in texts if piece and piece.strip())
    if not reply:
        reply = _FALLBACK_REPLY
    return QualificationDraft(
        reply=reply,
        request_human=_clean_bool(data.get("request_human")),
        destination=_clean_str(data.get("destination")),
        travel_dates=_clean_str(data.get("travel_dates")),
        group_size=_clean_int(data.get("group_size")),
        budget_usd=_clean_money(data.get("budget_usd")),
        contact_name=_clean_str(data.get("contact_name")),
        raw=data,
    )


def _compose_reply(
    *,
    latest: str,
    extracted: dict,
    known: dict,
    merged: dict,
    ad: dict | None,
    profile_name: str | None,
    history: list[dict],
    asked: bool,
) -> str:
    if asked:
        return (
            "Absolutely — I'll bring a person from the Horizon Sample Travel desk into this chat."
        )

    parts: list[str] = []
    first_user_turn = _user_turns(history) <= 1
    if profile_name and not known.get("contact_name") and first_user_turn:
        parts.append(f"Thanks, {profile_name}.")

    new_destination = extracted.get("destination")
    if new_destination and new_destination != known.get("destination"):
        package = package_for_destination(new_destination)
        if package:
            parts.append(
                f"{package['name']} is {package['duration_days']} days from "
                f"${package['price_from_usd']:,} (sample price). {package['summary']}"
            )
        else:
            parts.append(
                "I don't have a sample itinerary for that on this sheet, but I'll note it for the team."
            )
    elif ad and not merged.get("destination") and first_user_turn:
        headline = ad.get("headline") or "the ad you clicked"
        parts.append(f'I can plan around "{headline}", or look at something else.')

    question = _next_question(merged)
    if question:
        parts.append(question)
        return " ".join(parts)

    budget = merged["budget_usd"]
    parts.append(
        f"Lovely — I have {merged['destination']} for {merged['travel_dates']}, "
        f"{merged['group_size']} travellers, around ${budget:,.0f}, "
        f"under the name {merged['contact_name']}. "
        "A person from the Horizon Sample Travel desk will take it from here."
    )
    return " ".join(parts)


def _next_question(facts: dict) -> str | None:
    for key, question in _QUESTIONS:
        if facts.get(key) in (None, ""):
            return question
    return None


def _latest_user_text(history: list[dict]) -> str:
    for item in reversed(history):
        if item.get("role") == "user":
            return str(item.get("content") or "")
    return ""


def _user_turns(history: list[dict]) -> int:
    return sum(1 for item in history if item.get("role") == "user")


def _clean_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return False


def _clean_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 1 or number > 50:
        return None
    return number


def _clean_money(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 50:
        return None
    return round(number, 2)
