"""Turn a traveller message into a stored reply, an updated lead, and maybe a handoff.

Pipeline for each inbound text:
  1. Persist the message (Meta retries are ignored via provider message id).
  2. Ask the qualifier for a reply and any newly stated facts.
  3. Merge those facts onto the lead. Score in code, not in the model.
  4. Hand off when the score is hot or the traveller asks for a person.
  5. Deliver the reply through the messaging gateway and store it too.
"""

from __future__ import annotations

import json
import logging
import secrets

from app import knowledge
from app.db import Database
from app.llm import TurnContext
from app.messaging import DeliveryError, Inbound, MessagingGateway
from app.notify import StaffNotifier
from app.scoring import handoff_decision, merge_facts, score_lead, wants_human

logger = logging.getLogger("horizon.service")

HANDOFF_LINE = (
    "I've shared this with our travel specialists — a person from the "
    "Horizon Sample Travel desk will follow up in this chat shortly."
)
HANDOFF_LABELS = {
    "hot_lead": "Hot lead — with sales",
    "human_requested": "Asked for a person — with sales",
}
_APOLOGY = "Sorry — I couldn't read that just now. Please send it once more."
_NON_TEXT = (
    "I can read text in this demo. Send me a note about where you'd like to go "
    "and I'll take it from there."
)
_AFTER_HANDOFF = (
    "Thanks — I've added that to the note. A specialist already has this chat "
    "and will reply personally."
)


def build_system(known: dict, ad: dict | None, profile_name: str | None) -> str:
    if ad:
        ad_line = (
            f"This chat started from a {ad.get('source') or 'Meta'} ad "
            f"(id {ad.get('ad_id')}, campaign {ad.get('campaign') or 'unknown'}). "
            f"Headline: {ad.get('headline') or 'unknown'}. "
            "The ad is a hint, not a confirmed destination. Leave destination null "
            "until the traveller names or accepts a place."
        )
    else:
        ad_line = "No ad context. The traveller opened the chat directly."
    who = profile_name or "unknown"
    return f"""You are Maya, a travel specialist at Horizon Sample Travel, a fictional agency in a software demo.

You qualify WhatsApp leads that come from Meta click-to-WhatsApp ads. Be warm, brief, and specific, like a person typing in WhatsApp. Ask at most one question per message.

Rules:
- Use only the sample packages below. Never invent live availability, discounts, or destinations that are not on the sheet.
- If the sheet does not answer a question, say a specialist will confirm it.
- Collect destination, travel dates, group size, budget, and the name for the enquiry. Do not ask for a fact already on file.
- The WhatsApp profile name is {who}. You may greet them with it. Do not store it as contact_name until they state the booking name.
- When they ask for a human, set request_human to true and say you are bringing a person in.
- On every turn, call the tool record_lead_qualification. Put the WhatsApp reply in reply. Use null for unknown facts. Repeat facts already known so the record stays complete.

Ad context: {ad_line}

Facts already on file (JSON): {json.dumps(known, default=str)}

Sample packages:
{knowledge.prompt_block()}
"""


def opening_text(ad: dict | None, profile_name: str | None) -> str:
    hello = f"Hi {profile_name}!" if profile_name else "Hi!"
    if ad:
        source = ad.get("source") or "Meta"
        headline = ad.get("headline") or "our trip"
        return (
            f"{hello} I'm Maya at Horizon Sample Travel. "
            f"Thanks for tapping our {source} ad — \"{headline}\". "
            "I can walk you through that sample trip or help you compare a few others. "
            "Where are you hoping to go?"
        )
    return (
        f"{hello} I'm Maya at Horizon Sample Travel. "
        "Tell me where you'd like to go and I'll match a sample itinerary, "
        "then ask a few quick questions for our specialists."
    )


def start_simulated_chat(
    db: Database,
    gateway: MessagingGateway,
    *,
    ad_id: str | None,
    profile_name: str | None,
) -> dict:
    ad = None
    if ad_id:
        ad = knowledge.find_ad(ad_id)
        if ad is None:
            raise UnknownAd(ad_id)
    wa_id = _new_simulator_wa_id(db)
    conversation = db.create_conversation(
        wa_id=wa_id,
        profile_name=profile_name,
        source="simulator",
        ad=ad,
    )
    reply = opening_text(ad, profile_name)
    provider_id, delivery = deliver(gateway, wa_id, reply)
    db.add_message(conversation["id"], "out", reply, provider_id)
    snapshot = conversation_snapshot(db, conversation["id"])
    snapshot["reply"] = reply
    snapshot["delivery"] = delivery
    snapshot["duplicate"] = False
    return snapshot


class UnknownAd(LookupError):
    pass


def handle_incoming(
    db: Database,
    qualifier,
    gateway: MessagingGateway,
    notifier: StaffNotifier,
    inbound: Inbound,
) -> dict:
    if inbound.provider_message_id:
        existing = db.get_message_by_provider_id(inbound.provider_message_id)
        if existing:
            snapshot = conversation_snapshot(db, existing["conversation_id"])
            snapshot["reply"] = None
            snapshot["delivery"] = "duplicate"
            snapshot["duplicate"] = True
            return snapshot

    conversation = db.get_or_create_conversation(
        wa_id=inbound.wa_id,
        profile_name=inbound.profile_name,
        source=inbound.source,
    )
    if inbound.ad_id and not conversation["ad_id"]:
        conversation = db.attach_ad(conversation["id"], _resolved_ad(inbound))

    inbound_body = inbound.text if inbound.text else f"[{inbound.message_type} message]"
    db.add_message(conversation["id"], "in", inbound_body, inbound.provider_message_id)

    if conversation["status"] == "handed_off":
        return _finish(db, gateway, conversation, _AFTER_HANDOFF)

    if inbound.text is None:
        return _finish(db, gateway, conversation, _NON_TEXT)

    lead = db.get_lead_for_conversation(conversation["id"])
    if lead is None:
        raise RuntimeError(f"missing lead for conversation {conversation['id']}")
    known = facts_from_lead(lead)
    history = history_for_model(db.list_messages(conversation["id"]))
    ad = ad_out(conversation)
    # Profile may have been filled in by get_or_create after the first read.
    conversation = db.get_conversation(conversation["id"]) or conversation
    turn = TurnContext(
        history=history,
        known=known,
        ad=ad,
        profile_name=conversation.get("profile_name"),
        system=build_system(known, ad, conversation.get("profile_name")),
    )
    try:
        draft = qualifier.qualify(turn)
    except Exception:
        logger.exception("qualifier failed for conversation %s", conversation["id"])
        return _finish(db, gateway, conversation, _APOLOGY)

    facts = merge_facts(known, draft.facts())
    score = score_lead(facts)
    asked = bool(draft.request_human) or wants_human(inbound.text)
    handoff, handoff_reason = handoff_decision(score, asked)
    reply = (draft.reply or "").strip() or _APOLOGY
    if handoff:
        reply = with_handoff_line(reply)

    saved = db.save_lead(
        conversation["id"],
        {
            **facts,
            "score": score.label,
            "score_reason": score.reason,
            "handoff": handoff,
            "handoff_reason": handoff_reason,
            "extracted_json": json.dumps(
                {
                    "latest_tool_input": draft.raw or draft.facts(),
                    "merged": facts,
                    "score": score.label,
                    "score_reason": score.reason,
                    "handoff": handoff,
                    "handoff_reason": handoff_reason,
                },
                default=str,
            ),
        },
    )
    if handoff and conversation["status"] != "handed_off":
        db.set_status(conversation["id"], "handed_off")
        _notify(notifier, saved, conversation, score.label, handoff_reason, facts)

    return _finish(db, gateway, conversation, reply)


def with_handoff_line(reply: str) -> str:
    lowered = reply.lower()
    if "a person from" in lowered or "travel specialist" in lowered:
        return reply
    reply = reply.rstrip()
    if not reply:
        return HANDOFF_LINE
    return f"{reply} {HANDOFF_LINE}"


def conversation_snapshot(db: Database, conversation_id: int) -> dict:
    conversation = db.get_conversation(conversation_id)
    if conversation is None:
        raise KeyError(conversation_id)
    lead = db.get_lead_for_conversation(conversation_id)
    messages = db.list_messages(conversation_id)
    return {
        "conversation_id": conversation["id"],
        "wa_id": conversation["wa_id"],
        "profile_name": conversation["profile_name"],
        "status": conversation["status"],
        "ad": ad_out(conversation),
        "messages": [message_out(row) for row in messages],
        "lead": lead_out(lead) if lead else None,
    }


def facts_from_lead(lead: dict) -> dict:
    return {
        "destination": lead.get("destination"),
        "travel_dates": lead.get("travel_dates"),
        "group_size": lead.get("group_size"),
        "budget_usd": lead.get("budget_usd"),
        "contact_name": lead.get("contact_name"),
    }


def history_for_model(messages: list[dict]) -> list[dict]:
    history = []
    for message in messages:
        role = "user" if message["direction"] == "in" else "assistant"
        history.append({"role": role, "content": message["body"]})
    return history


def ad_out(conversation: dict) -> dict | None:
    if not conversation.get("ad_id"):
        return None
    return {
        "ad_id": conversation.get("ad_id"),
        "campaign": conversation.get("campaign"),
        "headline": conversation.get("ad_headline"),
        "source": conversation.get("ad_source"),
    }


def message_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "direction": row["direction"],
        "body": row["body"],
        "created_at": row["created_at"],
    }


def lead_out(row: dict) -> dict:
    raw = row.get("extracted_json") or "{}"
    try:
        extracted = json.loads(raw)
    except json.JSONDecodeError:
        extracted = {"unparsed": raw}
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "destination": row.get("destination"),
        "travel_dates": row.get("travel_dates"),
        "group_size": row.get("group_size"),
        "budget_usd": row.get("budget_usd"),
        "contact_name": row.get("contact_name"),
        "score": row.get("score"),
        "score_reason": row.get("score_reason"),
        "handoff": bool(row.get("handoff")),
        "handoff_reason": row.get("handoff_reason"),
        "extracted": extracted,
        "updated_at": row.get("updated_at"),
    }


def deliver(gateway: MessagingGateway, to: str, body: str) -> tuple[str | None, str]:
    try:
        return gateway.send_text(to, body), "sent"
    except (NotImplementedError, DeliveryError) as exc:
        logger.warning("delivery skipped for %s: %s", to, exc)
        return None, "skipped"


def _finish(db: Database, gateway: MessagingGateway, conversation: dict, reply: str) -> dict:
    provider_id, delivery = deliver(gateway, conversation["wa_id"], reply)
    db.add_message(conversation["id"], "out", reply, provider_id)
    snapshot = conversation_snapshot(db, conversation["id"])
    snapshot["reply"] = reply
    snapshot["delivery"] = delivery
    snapshot["duplicate"] = False
    return snapshot


def _notify(notifier, saved, conversation, score_label, handoff_reason, facts) -> None:
    summary = {
        "lead_id": saved["id"],
        "conversation_id": conversation["id"],
        "wa_id": conversation["wa_id"],
        "score": score_label,
        "reason": handoff_reason,
        "destination": facts.get("destination"),
        "contact_name": facts.get("contact_name"),
    }
    try:
        notifier.handoff(summary)
    except Exception:
        logger.exception("handoff notify failed for lead %s", saved["id"])


def _resolved_ad(inbound: Inbound) -> dict:
    catalog = knowledge.find_ad(inbound.ad_id)
    return {
        "ad_id": inbound.ad_id,
        "campaign": (catalog or {}).get("campaign"),
        "headline": inbound.ad_headline or (catalog or {}).get("headline"),
        "source": (catalog or {}).get("source") or inbound.ad_source,
    }


def _new_simulator_wa_id(db: Database) -> str:
    for _ in range(5):
        wa_id = f"sim{secrets.randbelow(900000) + 100000}"
        if db.get_conversation_by_wa(wa_id) is None:
            return wa_id
    raise RuntimeError("could not allocate a simulator number")
