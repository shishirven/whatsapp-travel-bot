"""Qualifiers: Claude via tool use, and a keyword stand-in for offline demos.

Claude Sonnet 5.5 rejects forced tool choice (`tool_choice` any/tool). The
call uses `auto` plus a strict schema, and the prompt tells the assistant to
call the tool every turn. If the model answers in plain text anyway, that
text is the reply and no new facts are stored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import anthropic

from app.business import Business, load_business
from app.extract import extract_facts
from app.scoring import canonical_facts, merge_facts, wants_human

logger = logging.getLogger("tradie.llm")

QUALIFY_TOOL = {
    "name": "record_job_intake",
    "description": (
        "Record what the customer has actually said about the job, and the "
        "exact WhatsApp reply to send. Call this on every turn. Use null for "
        "any fact they have not stated. Do not invent a suburb, a time, or a name. "
        "Never put a price in reply."
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
                    "Ask at most one question. Do not mention prices or dollar amounts."
                ),
            },
            "request_human": {
                "type": "boolean",
                "description": "True only when the customer asks to speak with a person.",
            },
            "job_type": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "Configured job type id, or their words, or null. The ad is not confirmation.",
            },
            "suburb": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "Suburb of the job, or null.",
            },
            "urgency": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "emergency, urgent, planned, or null.",
            },
            "description": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "What they say is wrong, in their words, or null.",
            },
            "preferred_time": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "When they would like the visit, in their words, or null.",
            },
            "contact_name": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "Name for the job, or null. Do not copy the WhatsApp profile.",
            },
        },
        "required": [
            "reply",
            "request_human",
            "job_type",
            "suburb",
            "urgency",
            "description",
            "preferred_time",
            "contact_name",
        ],
    },
}

_FALLBACK_REPLY = "Could you tell me a little more about the job?"
_MAX_HISTORY = 20
_SAFETY = "If anyone is in danger, or you can smell burning, get out and call 000."

_QUESTIONS = (
    ("job_type", "What needs doing — a fault, a power point, a switchboard, or something else?"),
    ("suburb", "Which suburb is the job in?"),
    ("urgency", "Is this an emergency, is it urgent, or can it wait?"),
    ("description", "What's going on, in a sentence or two?"),
    ("contact_name", "What name should I put on the job?"),
)


@dataclass
class QualificationDraft:
    """One turn: the WhatsApp reply, plus any facts stated in that turn."""

    reply: str
    request_human: bool = False
    job_type: str | None = None
    suburb: str | None = None
    urgency: str | None = None
    description: str | None = None
    preferred_time: str | None = None
    contact_name: str | None = None
    raw: dict | None = None

    def facts(self) -> dict:
        return {
            "job_type": self.job_type,
            "suburb": self.suburb,
            "urgency": self.urgency,
            "description": self.description,
            "preferred_time": self.preferred_time,
            "contact_name": self.contact_name,
        }


@dataclass
class TurnContext:
    """What a qualifier may look at for a single customer message."""

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

    def __init__(self, business: Business | None = None) -> None:
        self.business = business or load_business()

    def qualify(self, turn: TurnContext) -> QualificationDraft:
        latest = _latest_user_text(turn.history)
        extracted = extract_facts(latest, self.business)
        merged = canonical_facts(merge_facts(turn.known, extracted), self.business)
        asked = wants_human(latest)
        reply = _compose_reply(
            business=self.business,
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
            job_type=extracted["job_type"],
            suburb=extracted["suburb"],
            urgency=extracted["urgency"],
            description=extracted["description"],
            preferred_time=extracted["preferred_time"],
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
                "content": "(The customer opened the chat. Your greeting was already sent.)",
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
    urgency = _clean_str(data.get("urgency"))
    if urgency:
        urgency = urgency.lower()
    return QualificationDraft(
        reply=reply,
        request_human=_clean_bool(data.get("request_human")),
        job_type=_clean_str(data.get("job_type")),
        suburb=_clean_str(data.get("suburb")),
        urgency=urgency,
        description=_clean_str(data.get("description")),
        preferred_time=_clean_str(data.get("preferred_time")),
        contact_name=_clean_str(data.get("contact_name")),
        raw=data,
    )


def _compose_reply(
    *,
    business: Business,
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
            f"Absolutely — I'll bring a person from the {business.name} desk into this chat."
        )

    parts: list[str] = []
    if merged.get("urgency") == "emergency" and "000" not in latest:
        parts.append(_SAFETY)

    first_user_turn = _user_turns(history) <= 1
    if profile_name and not known.get("contact_name") and first_user_turn:
        parts.append(f"Thanks, {profile_name}.")

    new_job = extracted.get("job_type")
    if new_job and new_job != known.get("job_type"):
        label = business.job_label(new_job) or new_job
        parts.append(f"{label} — I'll put that on the job.")
    elif ad and not merged.get("job_type") and first_user_turn:
        headline = ad.get("headline") or "the ad you tapped"
        parts.append(f'I can help with "{headline}", or with something else.')

    if merged.get("suburb") and not business.in_area(merged.get("suburb")) and extracted.get("suburb"):
        parts.append(
            f"{merged['suburb']} is outside the area we cover. "
            "I'll note it so someone can refer you."
        )

    question = _next_question(merged)
    if question and not (merged.get("urgency") == "emergency" and business.in_area(merged.get("suburb")) and merged.get("job_type")):
        # An in-area emergency is already enough to hand over. Don't pile on a question.
        if not _checklist_complete(merged, business):
            parts.append(question)
            return " ".join(parts)

    if _checklist_complete(merged, business) or (
        merged.get("urgency") == "emergency" and business.in_area(merged.get("suburb")) and merged.get("job_type")
    ):
        label = business.job_label(merged.get("job_type")) or merged.get("job_type")
        who = merged.get("contact_name") or "you"
        parts.append(
            f"I have a {label} in {merged.get('suburb')} ({merged.get('urgency') or 'emergency'}) "
            f"under the name {who}. A person from the {business.name} desk will take it from here."
        )
        return " ".join(parts)

    if question:
        parts.append(question)
    return " ".join(parts) or _FALLBACK_REPLY


def _checklist_complete(facts: dict, business: Business) -> bool:
    return bool(
        business.resolve_job_type(facts.get("job_type"))
        and business.in_area(facts.get("suburb"))
        and facts.get("urgency")
        and facts.get("description")
        and facts.get("contact_name")
    )


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
