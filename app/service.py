"""Turn a customer message into a stored reply, an updated job, and maybe a handoff.

Pipeline for each inbound message:
  1. Persist the message (Meta retries are ignored via provider message id).
  2. Download a photo or voice note, and transcribe audio when that is what arrived.
  3. If they pick one of the offered visit slots, book it.
  4. Ask the qualifier for a reply and any newly stated facts.
  5. Merge those facts onto the job. Score in code, not in the model.
  6. Keep a draft quote on the job. Never put the price in the customer reply.
  7. Hand off when the score is hot or the customer asks for a person.
  8. Offer three free slots once the job type and suburb are in area.
  9. Deliver the reply. Free-form text only inside the 24-hour window.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from app.booking import (
    Calendar,
    DatabaseCalendar,
    Slot,
    available_slots,
    booking_from_slots,
    format_confirmation,
    format_offer,
    mark_booked,
    match_slot,
    offered_slots,
)
from app.business import Business, load_business
from app.db import Database
from app.llm import TurnContext
from app.media import (
    MediaDownloader,
    MediaError,
    MediaNotConfigured,
    Transcriber,
    save_media,
    summarise_transcript,
)
from app.messaging import DeliveryError, Inbound, MessagingGateway
from app.notify import StaffNotifier
from app.quotes import customer_quote_text, draft_quote, scrub_prices
from app.routing import (
    KeywordIntentClassifier,
    classify_source,
    evaluate_soft_match,
    is_status_question,
    job_summary,
    status_reply,
    suburb_in_address,
)
from app.scoring import canonical_facts, handoff_decision, merge_facts, score_lead, wants_human
from app.window import outbound_plan, parse_stored_time

logger = logging.getLogger("tradie.service")

HANDOFF_LABELS = {
    "hot_lead": "Hot job — with the electrician",
    "human_requested": "Asked for a person — with the electrician",
}
_APOLOGY = "Sorry — I couldn't read that just now. Please send it once more."
_NON_TEXT = (
    "Send a text, a photo, or a voice note about the job and the suburb, "
    "and I'll take it from there."
)
_AFTER_HANDOFF = (
    "Thanks — I've added that to the job. A specialist already has this chat "
    "and will reply personally."
)
_PHOTO_ACK = (
    "I've put that photo on the job. Tell me the suburb and what's going on if you haven't already."
)
_VOICE_UNAVAILABLE = (
    "I got your voice note, but I couldn't transcribe it just now. "
    "Type the suburb and what's going on if you can."
)
_SAFETY = "If anyone is in danger, or you can smell burning, get out and call 000."


def build_system(
    known: dict,
    ad: dict | None,
    profile_name: str | None,
    business: Business | None = None,
    *,
    saved_address: str | None = None,
    established: bool = False,
) -> str:
    business = business or load_business()
    if established and saved_address:
        relationship = (
            "This WhatsApp number is already a client. "
            f"Saved address: {saved_address}. "
            "Greet them by name and confirm that address. "
            "Ask only for facts that are not already on file. "
            "Do not mention prices, quotes, or any other customer's jobs."
        )
    elif established:
        relationship = (
            "This WhatsApp number is already a client. "
            "Greet them by name if you know it, and ask only for facts that are not already on file. "
            "Do not mention prices, quotes, or any other customer's jobs."
        )
    else:
        relationship = (
            "This WhatsApp number is not a confirmed existing client. "
            "Do not say you recognise them. "
            "Do not mention previous jobs, quotes, addresses, or bookings."
        )
    if ad:
        ad_line = (
            f"This chat started from a {ad.get('source') or 'Meta'} ad "
            f"(id {ad.get('ad_id')}, campaign {ad.get('campaign') or 'unknown'}). "
            f"Headline: {ad.get('headline') or 'unknown'}. "
            "The ad is a hint, not a confirmed job. Leave job_type null "
            "until the customer describes the work."
        )
    else:
        ad_line = "No ad context. The customer opened the chat directly."
    who = profile_name or "unknown"
    return f"""You are {business.assistant_name}, the WhatsApp assistant for {business.name}, a {business.trade} serving {business.area_name}.

You take job details for the desk. Be brief and specific, like a person typing in WhatsApp. Ask at most one question per message.

Rules:
- Collect job type, suburb, urgency, a short description, preferred time, and the customer's name. Do not ask for a fact already on file.
- Urgency is one of emergency, urgent, or planned.
- If the suburb is outside the service area, say so and that a person can refer them. Still record the suburb.
- If someone may be in danger, or they report burning, smoke, or a shock, tell them to leave and call 000, then keep the job details.
- Do not mention prices, dollar amounts, or quote ranges. Do not invent a price. A person approves every quote before the customer sees it.
- The WhatsApp profile name is {who}. You may greet them with it. Do not store it as contact_name until they state their name.
- When they ask for a human, set request_human to true and say you are bringing a person in.
- On every turn, call the tool record_job_intake. Put the WhatsApp reply in reply. Use null for unknown facts. Repeat facts already known so the record stays complete.
- The ad is a hint, not a confirmed job.

Ad context: {ad_line}

{relationship}

Facts already on file (JSON): {json.dumps(known, default=str)}

{business.prompt_block()}
"""


def opening_text(business: Business, ad: dict | None, profile_name: str | None) -> str:
    hello = f"Hi {profile_name}!" if profile_name else "Hi!"
    who = f"I'm {business.assistant_name} at {business.name}."
    if ad:
        source = ad.get("source") or "Meta"
        headline = ad.get("headline") or "our ad"
        return (
            f"{hello} {who} "
            f"Thanks for tapping our {source} ad — \"{headline}\". "
            "Tell me what needs doing and the suburb, and I'll line up a visit."
        )
    return (
        f"{hello} {who} "
        "Tell me what needs doing and which suburb you're in, "
        "and I'll take the job details for the electrician."
    )


def start_simulated_chat(
    db: Database,
    gateway: MessagingGateway,
    *,
    ad_id: str | None,
    profile_name: str | None,
    business: Business | None = None,
    now: datetime | None = None,
) -> dict:
    business = business or load_business()
    moment = now or datetime.now(timezone.utc)
    ad = None
    if ad_id:
        found = business.find_ad(ad_id)
        if found is None:
            raise UnknownAd(ad_id)
        ad = _ad_dict(found)
    wa_id = _new_simulator_wa_id(db)
    conversation = db.create_conversation(
        wa_id=wa_id,
        profile_name=profile_name,
        source="simulator",
        ad=ad,
        job_source="ad" if ad else "organic",
    )
    reply = opening_text(business, ad, profile_name)
    _deliver_and_store(
        db,
        gateway,
        conversation,
        reply,
        purpose="session",
        last_customer_at=moment,
        now=moment,
        job_id=conversation.get("active_job_id"),
    )
    snapshot = conversation_snapshot(db, conversation["id"], business)
    snapshot["reply"] = reply
    snapshot["delivery"] = "sent"
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
    *,
    business: Business | None = None,
    calendar: Calendar | None = None,
    media: MediaDownloader | None = None,
    transcriber: Transcriber | None = None,
    now: datetime | None = None,
    intent=None,
) -> dict:
    business = business or load_business()
    moment = now or datetime.now(timezone.utc)
    if calendar is None:
        calendar = DatabaseCalendar(db)
    if intent is None:
        intent = KeywordIntentClassifier()
    if inbound.provider_message_id:
        existing_message = db.get_message_by_provider_id(inbound.provider_message_id)
        if existing_message:
            snapshot = conversation_snapshot(db, existing_message["conversation_id"], business)
            snapshot["reply"] = None
            snapshot["delivery"] = "duplicate"
            snapshot["duplicate"] = True
            return snapshot

    existing = db.get_conversation_by_wa(inbound.wa_id)
    known_customer = db.get_customer_by_wa(inbound.wa_id)
    source, cleaned = classify_source(
        inbound.text,
        inbound=inbound,
        returning=known_customer is not None,
        business=business,
    )
    ad = _resolved_ad(inbound, business) if inbound.ad_id or inbound.ad_headline else None
    route = "follow_up"
    if existing is None and known_customer is not None:
        prefill, saved_address = _prefill_known(known_customer, business)
        conversation = db.create_conversation(
            wa_id=inbound.wa_id,
            profile_name=inbound.profile_name,
            source=inbound.source,
            ad=ad,
            job_source=source,
            customer_id=known_customer["id"],
            prefill=prefill,
            address=saved_address,
        )
        lead = db.get_job(conversation["active_job_id"])
        route = "returning"
    elif existing is None:
        conversation = db.create_conversation(
            wa_id=inbound.wa_id,
            profile_name=inbound.profile_name,
            source=inbound.source,
            ad=ad,
            job_source=source,
        )
        lead = db.get_job(conversation["active_job_id"])
        route = "new"
    else:
        conversation = existing
        if inbound.profile_name and not conversation.get("profile_name"):
            conversation = db.set_profile_name(conversation["id"], inbound.profile_name)
        lead = db.get_open_job(conversation["id"])
        if lead is None:
            customer = db.get_customer(conversation["customer_id"])
            prefill, saved_address = _prefill_known(customer, business)
            lead = db.create_job(
                customer_id=conversation["customer_id"],
                conversation_id=conversation["id"],
                source=source,
                ad=ad,
                contact_name=prefill.get("contact_name"),
                suburb=prefill.get("suburb"),
                address=saved_address,
            )
            conversation = db.set_status(conversation["id"], "open")
            route = "returning"
    if lead is None:
        raise RuntimeError(f"missing job for conversation {conversation['id']}")

    photos = _load_json(lead.get("photos_json"), [])
    voice_notes = _load_json(lead.get("voice_json"), [])
    booking = _load_json(lead.get("booking_json"), {})
    quote = _load_json(lead.get("quote_json"), {})
    photo_count = len(photos)
    voice_count = len(voice_notes)

    text, photos, voice_notes, body = _ingest_media(
        inbound,
        photos,
        voice_notes,
        media_dir=Path(db.path).parent / "media",
        downloader=media,
        transcriber=transcriber,
    )
    if inbound.message_type != "audio" and inbound.text:
        text = cleaned.strip() if cleaned and cleaned.strip() else "Hi"
    elif inbound.message_type == "audio":
        pass

    if route == "follow_up" and _starts_another_job(db, intent, lead, text, booking, moment):
        customer = db.get_customer(conversation["customer_id"])
        prefill, saved_address = _prefill_known(customer, business)
        fresh_source, _ignored = classify_source(
            inbound.text,
            inbound=inbound,
            returning=True,
            business=business,
        )
        lead = db.create_job(
            customer_id=conversation["customer_id"],
            conversation_id=conversation["id"],
            source=fresh_source,
            ad=ad,
            contact_name=prefill.get("contact_name"),
            suburb=prefill.get("suburb"),
            address=saved_address,
        )
        photos = photos[photo_count:]
        voice_notes = voice_notes[voice_count:]
        booking = {}
        quote = {}
        conversation = db.set_status(conversation["id"], "open")
        route = "another"

    db.add_message(conversation["id"], "in", body, inbound.provider_message_id, job_id=lead["id"])
    # The message we just stored is the customer's latest message, so a reply
    # in this turn is inside the 24-hour window.
    last_customer_at = parse_stored_time(db.last_inbound_at(conversation["id"])) or moment
    opening = _opening_prefix(db, business, conversation, lead, route)

    if route == "follow_up" and lead.get("handoff"):
        booked_reply, booking = _maybe_book(
            text, booking, business, calendar, lead, conversation, moment
        )
        if booked_reply:
            _save(
                db, business, conversation, lead, photos, voice_notes, booking, quote,
                facts=_facts_of(lead), score_label=lead["score"], score_reason=lead["score_reason"],
                handoff=bool(lead["handoff"]), handoff_reason=lead.get("handoff_reason"),
                raw=None,
            )
            return _finish(
                db, gateway, conversation, booked_reply, business,
                purpose="booking_confirmation", last_customer_at=last_customer_at, now=moment,
                job_id=lead["id"],
            )
        if is_status_question(text):
            reply = status_reply(lead, booking, quote)
        elif inbound.message_type == "image":
            reply = _PHOTO_ACK
        elif inbound.message_type == "audio" and not text:
            reply = _VOICE_UNAVAILABLE
        else:
            reply = _AFTER_HANDOFF
        _save(
            db, business, conversation, lead, photos, voice_notes, booking, quote,
            facts=_facts_of(lead), score_label=lead["score"], score_reason=lead["score_reason"],
            handoff=True, handoff_reason=lead.get("handoff_reason"),
            raw=None,
        )
        return _finish(
            db, gateway, conversation, reply, business,
            purpose="session", last_customer_at=last_customer_at, now=moment,
            job_id=lead["id"],
        )

    booked_reply, booking = _maybe_book(
        text, booking, business, calendar, lead, conversation, moment
    )

    if text is None:
        _save(
            db, business, conversation, lead, photos, voice_notes, booking, quote,
            facts=_facts_of(lead), score_label=lead["score"], score_reason=lead["score_reason"],
            handoff=bool(lead["handoff"]), handoff_reason=lead.get("handoff_reason"),
            raw=None,
        )
        reply = _PHOTO_ACK if inbound.message_type == "image" else _NON_TEXT
        if inbound.message_type == "audio":
            reply = _VOICE_UNAVAILABLE
        return _finish(
            db, gateway, conversation, _with_opening(opening, reply), business,
            purpose="session", last_customer_at=last_customer_at, now=moment,
            job_id=lead["id"],
        )

    known = _facts_of(lead)
    history = history_for_model(db.list_messages(conversation["id"], job_id=lead["id"]))
    job_ad = _job_ad(lead)
    customer = db.get_customer(lead["customer_id"])
    established = _is_established(db, customer, lead["id"])
    saved_address = _saved_address(customer) if established else None
    conversation = db.get_conversation(conversation["id"]) or conversation
    turn = TurnContext(
        history=history,
        known=known,
        ad=job_ad,
        profile_name=conversation.get("profile_name"),
        system=build_system(
            known,
            job_ad,
            conversation.get("profile_name"),
            business,
            saved_address=saved_address,
            established=established,
        ),
    )
    try:
        draft = qualifier.qualify(turn)
    except Exception:
        logger.exception("qualifier failed for conversation %s", conversation["id"])
        _save(
            db, business, conversation, lead, photos, voice_notes, booking, quote,
            facts=known, score_label=lead["score"], score_reason=lead["score_reason"],
            handoff=bool(lead["handoff"]), handoff_reason=lead.get("handoff_reason"),
            raw=None,
        )
        return _finish(
            db, gateway, conversation, _with_opening(opening, _APOLOGY), business,
            purpose="session", last_customer_at=last_customer_at, now=moment,
            job_id=lead["id"],
        )

    facts = canonical_facts(merge_facts(known, draft.facts()), business)
    score = score_lead(facts, business)
    asked = bool(draft.request_human) or wants_human(text)
    handoff, handoff_reason = handoff_decision(score, asked)
    quote = draft_quote(business, facts.get("job_type"), quote or None)
    reply = scrub_prices((draft.reply or "").strip()) or _APOLOGY
    if facts.get("urgency") == "emergency" and "000" not in reply:
        reply = f"{_SAFETY} {reply}"
    soft_question = _apply_soft_match(
        db, lead, facts, text, established=established
    )
    if soft_question and not booked_reply:
        reply = soft_question
        if facts.get("urgency") == "emergency" and "000" not in reply:
            reply = f"{_SAFETY} {reply}"
    elif booked_reply:
        reply = f"{booked_reply} {reply}".strip()
    elif _should_offer(facts, booking, business, moment):
        slots = available_slots(business, calendar, moment) if calendar is not None else []
        if slots:
            booking = booking_from_slots(slots)
            reply = _with_offer(reply, slots, facts.get("preferred_time"))
    if handoff:
        reply = with_handoff_line(reply, business)
    reply = scrub_prices(_with_opening(opening, reply)) or _APOLOGY

    saved = _save(
        db, business, conversation, lead, photos, voice_notes, booking, quote,
        facts=facts, score_label=score.label, score_reason=score.reason,
        handoff=handoff, handoff_reason=handoff_reason, raw=draft.raw or draft.facts(),
    )
    if handoff and conversation["status"] != "handed_off":
        db.set_status(conversation["id"], "handed_off")
        _notify(notifier, saved, conversation, score.label, handoff_reason, facts)

    purpose = "booking_confirmation" if booked_reply else "session"
    return _finish(
        db, gateway, conversation, reply, business,
        purpose=purpose, last_customer_at=last_customer_at, now=moment,
        job_id=lead["id"],
    )


def approve_quote(
    db: Database,
    gateway: MessagingGateway,
    lead_id: int,
    *,
    business: Business | None = None,
    now: datetime | None = None,
) -> dict:
    """Send a draft quote. Refuses to send twice. Uses a template outside 24h."""
    business = business or load_business()
    moment = now or datetime.now(timezone.utc)
    lead = db.get_lead(lead_id)
    if lead is None:
        raise KeyError(lead_id)
    quote = _load_json(lead.get("quote_json"), {})
    if not quote or not quote.get("min_aud"):
        raise QuoteNotReady("This job has no draft quote yet.")
    if quote.get("status") == "sent":
        return {"sent": False, "reason": "already_sent", "lead_id": lead_id}
    body = customer_quote_text(
        business,
        quote,
        contact_name=lead.get("contact_name"),
        suburb=lead.get("suburb"),
    )
    conversation = db.get_conversation(lead["conversation_id"])
    if conversation is None:
        raise KeyError(lead_id)
    last_customer_at = parse_stored_time(db.last_inbound_at(conversation["id"]))
    plan = outbound_plan(last_customer_at, moment, "quote_followup")
    provider_id, delivery, stored = deliver_customer(
        gateway,
        conversation["wa_id"],
        body,
        purpose="quote_followup",
        last_customer_at=last_customer_at,
        now=moment,
        template_params=_quote_params(lead, quote),
    )
    if delivery != "sent":
        logger.info("quote send skipped lead=%s mode=%s", lead_id, plan["mode"])
        return {"sent": False, "reason": delivery, "lead_id": lead_id, "mode": plan["mode"]}
    quote = dict(quote)
    quote["status"] = "sent"
    quote["sent_via"] = plan["mode"]
    db.add_message(conversation["id"], "out", stored, provider_id, job_id=lead["id"])
    facts = _facts_of(lead)
    _save(
        db, business, conversation, lead,
        _load_json(lead.get("photos_json"), []),
        _load_json(lead.get("voice_json"), []),
        _load_json(lead.get("booking_json"), {}),
        quote,
        facts=facts,
        score_label=lead["score"],
        score_reason=lead["score_reason"],
        handoff=bool(lead["handoff"]),
        handoff_reason=lead.get("handoff_reason"),
        raw=_load_json(lead.get("extracted_json"), {}),
    )
    logger.info("quote sent lead=%s mode=%s", lead_id, plan["mode"])
    return {"sent": True, "mode": plan["mode"], "lead_id": lead_id}


class QuoteNotReady(LookupError):
    pass


def with_handoff_line(reply: str, business: Business | None = None) -> str:
    business = business or load_business()
    line = (
        f"I've shared this with the team — a person from the {business.name} desk "
        "will follow up in this chat shortly."
    )
    lowered = reply.lower()
    if "a person from" in lowered:
        return reply
    reply = reply.rstrip()
    if not reply:
        return line
    return f"{reply} {line}"


def deliver_customer(
    gateway: MessagingGateway,
    to: str,
    body: str,
    *,
    purpose: str,
    last_customer_at: datetime | None,
    now: datetime,
    template_params: list[str] | None = None,
) -> tuple[str | None, str, str]:
    """Send free-form text inside the 24h window, otherwise a template stub.

    Returns provider id, delivery status (`sent`, `skipped`, or `held`), and
    the transcript body to store.
    """
    plan = outbound_plan(last_customer_at, now, purpose)
    if plan["mode"] == "text":
        provider_id, status = _send_text(gateway, to, body)
        return provider_id, status, body
    if plan["mode"] == "held":
        logger.info("held free-form reply to %s; outside the 24h window", to)
        note = "[not sent — outside the 24h window; a template is required]"
        return None, "held", note
    params = template_params if template_params is not None else [body[:200]]
    template = plan["template"]
    try:
        provider_id = gateway.send_template(to, template, params)
        status = "sent"
    except (NotImplementedError, DeliveryError) as exc:
        logger.warning("template delivery skipped for %s: %s", to, exc)
        provider_id = None
        status = "skipped"
    stored = f"[template:{template}] " + " | ".join(params)
    return provider_id, status, stored


def conversation_snapshot(db: Database, conversation_id: int, business: Business | None = None) -> dict:
    business = business or load_business()
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
        "ad": _job_ad(lead) if lead and _job_ad(lead) else ad_out(conversation),
        "messages": [message_out(row) for row in messages],
        "lead": lead_out(lead, business) if lead else None,
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


def lead_out(row: dict, business: Business | None = None) -> dict:
    business = business or load_business()
    raw = row.get("extracted_json") or "{}"
    try:
        extracted = json.loads(raw)
    except json.JSONDecodeError:
        extracted = {"unparsed": raw}
    job_type = row.get("job_type")
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "job_type": job_type,
        "job_label": business.job_label(job_type) if job_type else None,
        "suburb": row.get("suburb"),
        "urgency": row.get("urgency"),
        "description": row.get("description"),
        "preferred_time": row.get("preferred_time"),
        "contact_name": row.get("contact_name"),
        "score": row.get("score"),
        "score_reason": row.get("score_reason"),
        "handoff": bool(row.get("handoff")),
        "handoff_reason": row.get("handoff_reason"),
        "photos": _load_json(row.get("photos_json"), []),
        "voice_notes": _load_json(row.get("voice_json"), []),
        "quote": _load_json(row.get("quote_json"), {}) or None,
        "booking": _load_json(row.get("booking_json"), {}) or None,
        "extracted": extracted,
        "updated_at": row.get("updated_at"),
        "wa_id": row.get("wa_id"),
        "profile_name": row.get("profile_name"),
        "source": row.get("job_source") or row.get("source"),
        "status": row.get("job_status") or row.get("status") or "open",
        "customer_id": row.get("customer_id"),
        "customer_name": row.get("customer_name"),
        "address": row.get("address"),
        "possible_customer_id": row.get("possible_customer_id"),
        "possible_customer_name": row.get("possible_customer_name"),
        "ad_id": row.get("ad_id"),
        "campaign": row.get("campaign"),
        "ad_headline": row.get("ad_headline"),
        "ad_source": row.get("ad_source"),
        "last_message": row.get("last_message"),
    }


def _finish(
    db: Database,
    gateway: MessagingGateway,
    conversation: dict,
    reply: str,
    business: Business,
    *,
    purpose: str,
    last_customer_at: datetime | None,
    now: datetime,
    job_id: int | None = None,
) -> dict:
    _deliver_and_store(
        db, gateway, conversation, reply,
        purpose=purpose, last_customer_at=last_customer_at, now=now,
        job_id=job_id,
    )
    snapshot = conversation_snapshot(db, conversation["id"], business)
    snapshot["reply"] = reply
    snapshot["delivery"] = "sent"
    snapshot["duplicate"] = False
    return snapshot


def _deliver_and_store(
    db, gateway, conversation, reply, *, purpose, last_customer_at, now, template_params=None,
    job_id=None,
) -> None:
    provider_id, _status, stored = deliver_customer(
        gateway,
        conversation["wa_id"],
        reply,
        purpose=purpose,
        last_customer_at=last_customer_at,
        now=now,
        template_params=template_params,
    )
    db.add_message(conversation["id"], "out", stored, provider_id, job_id=job_id)


def _ingest_media(inbound, photos, voice_notes, *, media_dir, downloader, transcriber):
    if inbound.message_type == "audio" and inbound.media_id:
        try:
            downloaded = _download(downloader, inbound.media_id)
            path = save_media(media_dir, downloaded) if downloaded else None
            if transcriber is None or downloaded is None:
                raise MediaNotConfigured("transcription unavailable")
            transcript = transcriber.transcribe(
                downloaded.data,
                filename=downloaded.filename,
                mime_type=downloaded.mime_type,
            )
        except (MediaError, MediaNotConfigured):
            logger.info("voice note unavailable media_id=%s", inbound.media_id)
            return None, photos, voice_notes, "[audio message]"
        except Exception:
            logger.exception("voice note failed media_id=%s", inbound.media_id)
            return None, photos, voice_notes, "[audio message]"
        summary = summarise_transcript(transcript)
        voice_notes = list(voice_notes) + [
            {
                "media_id": inbound.media_id,
                "mime_type": downloaded.mime_type,
                "path": path,
                "transcript": transcript,
                "summary": summary,
            }
        ]
        return transcript, photos, voice_notes, f"Voice note: {transcript}"

    if inbound.message_type == "image" and inbound.media_id:
        caption = inbound.text
        record = {"media_id": inbound.media_id, "mime_type": inbound.mime_type, "caption": caption}
        try:
            downloaded = _download(downloader, inbound.media_id)
            if downloaded is not None:
                record["path"] = save_media(media_dir, downloaded)
                record["mime_type"] = downloaded.mime_type
                record["status"] = "saved"
            else:
                record["status"] = "not_downloaded"
        except MediaNotConfigured:
            record["status"] = "not_downloaded"
        except Exception:
            logger.exception("image download failed media_id=%s", inbound.media_id)
            record["status"] = "failed"
        photos = list(photos) + [record]
        body = "Photo attached"
        if caption:
            body = f"{body}: {caption}"
        else:
            body = f"{body} (media {inbound.media_id})"
        return caption, photos, voice_notes, body

    if inbound.text:
        return inbound.text, photos, voice_notes, inbound.text
    return None, photos, voice_notes, f"[{inbound.message_type} message]"


def _download(downloader, media_id):
    if downloader is None:
        raise MediaNotConfigured("no downloader")
    return downloader.download(media_id)


def _maybe_book(text, booking, business, calendar, lead, conversation, moment):
    slots = offered_slots(booking, moment)
    if not slots or calendar is None:
        return None, booking
    chosen = match_slot(text, slots)
    if chosen is None:
        return None, booking
    if not _slot_free(calendar, chosen):
        fresh = available_slots(business, calendar, moment)
        if not fresh:
            return (
                "That time was just taken, and I can't see another opening. "
                "A person will propose a time.",
                {"status": "offered", "slots": []},
            )
        return (
            "That time was just taken.\n\n" + format_offer(fresh),
            booking_from_slots(fresh),
        )
    summary = _event_summary(business, lead, chosen)
    description = _event_description(lead, conversation)
    try:
        event_id = calendar.create_event(chosen.start, chosen.end, summary, description)
    except Exception:
        logger.exception("booking failed lead=%s", lead.get("id"))
        return (
            "I couldn't lock that time just now. Reply with 1, 2, or 3 again in a moment.",
            booking,
        )
    return format_confirmation(chosen), mark_booked(booking, chosen, event_id)


def _slot_free(calendar: Calendar, slot: Slot) -> bool:
    try:
        busy = calendar.busy_intervals(slot.start, slot.end)
    except Exception:
        logger.exception("calendar busy lookup failed before booking")
        return True
    for begin, finish in busy:
        if begin.tzinfo and slot.start.tzinfo:
            begin = begin.astimezone(slot.start.tzinfo)
        if finish.tzinfo and slot.start.tzinfo:
            finish = finish.astimezone(slot.start.tzinfo)
        if slot.start < finish and slot.end > begin:
            return False
    return True


def _should_offer(facts: dict, booking: dict, business: Business, now: datetime) -> bool:
    if booking.get("status") == "booked":
        return False
    if booking.get("status") == "offered" and offered_slots(booking, now):
        return False
    return bool(
        business.resolve_job_type(facts.get("job_type"))
        and business.in_area(facts.get("suburb"))
    )


def _with_offer(reply: str, slots: list[Slot], preferred: str | None) -> str:
    """Keep the acknowledgement and let the slot list be the only question."""
    offer = format_offer(slots, preferred)
    sentences = re.split(r"(?<=[.!?])\s+", (reply or "").strip())
    if sentences and "?" in sentences[-1]:
        sentences = sentences[:-1]
    kept = " ".join(sentence for sentence in sentences if sentence).strip()
    if kept:
        return f"{kept}\n\n{offer}"
    return offer


def _save(
    db, business, conversation, lead, photos, voice_notes, booking, quote,
    *, facts, score_label, score_reason, handoff, handoff_reason, raw,
) -> dict:
    saved = db.save_job(
        lead["id"],
        {
            **facts,
            "score": score_label,
            "score_reason": score_reason,
            "handoff": handoff,
            "handoff_reason": handoff_reason,
            "photos_json": json.dumps(photos),
            "voice_json": json.dumps(voice_notes),
            "quote_json": json.dumps(quote or {}),
            "booking_json": json.dumps(booking or {}),
            "extracted_json": json.dumps(
                {
                    "latest_tool_input": raw or facts,
                    "merged": facts,
                    "score": score_label,
                    "score_reason": score_reason,
                    "handoff": handoff,
                    "handoff_reason": handoff_reason,
                },
                default=str,
            ),
        },
    )
    db.note_customer_facts(
        saved["customer_id"],
        contact_name=facts.get("contact_name"),
        profile_name=conversation.get("profile_name"),
        suburb=facts.get("suburb"),
        address=saved.get("address"),
    )
    return saved


def _starts_another_job(db, intent, lead, text, booking, moment) -> bool:
    """True when an open job should stay put and this message starts a new one."""
    if not text or not db.job_has_inbound(lead["id"]):
        return False
    if wants_human(text) or is_status_question(text):
        return False
    slots = offered_slots(booking, moment)
    if slots and match_slot(text, slots) is not None:
        return False
    try:
        return bool(intent.classify(text, job_summary=job_summary(lead)))
    except Exception:
        logger.exception("intent classify failed for job %s", lead.get("id"))
        return False


def _prefill_known(customer: dict | None, business: Business) -> tuple[dict, str | None]:
    if not customer:
        return {}, None
    saved = _saved_address(customer)
    facts: dict = {}
    if customer.get("name") and (customer.get("name_confirmed") or customer.get("trusted")):
        facts["contact_name"] = customer["name"]
    suburb = suburb_in_address(saved, business) if saved else None
    if suburb:
        facts["suburb"] = suburb
    return facts, saved


def _saved_address(customer: dict | None) -> str | None:
    if not customer:
        return None
    addresses = _load_json(customer.get("addresses_json"), [])
    if isinstance(addresses, list) and addresses:
        return str(addresses[0])
    return None


def _is_established(db: Database, customer: dict | None, job_id: int) -> bool:
    if not customer:
        return False
    if customer.get("trusted"):
        return True
    return db.customer_has_other_job(customer["id"], job_id)


def _apply_soft_match(db, lead, facts, text, *, established: bool) -> str | None:
    if established:
        return None
    customer_id = lead.get("customer_id")
    if not customer_id:
        return None
    soft = evaluate_soft_match(
        name=facts.get("contact_name"),
        suburb=facts.get("suburb"),
        address=lead.get("address"),
        text=text,
        asked=bool(lead.get("prior_client_asked")),
        answer=lead.get("prior_client_answer"),
        address_prompted=bool(lead.get("address_prompted")),
        candidates=db.list_match_candidates(customer_id),
    )
    if soft["answer"] == "no":
        possible_id = None
    elif soft["possible_customer_id"]:
        possible_id = soft["possible_customer_id"]
    else:
        possible_id = lead.get("possible_customer_id")
    db.set_soft_match(
        lead["id"],
        possible_customer_id=possible_id,
        asked=soft["asked"],
        answer=soft["answer"],
        address_prompted=soft["address_prompted"],
    )
    return soft["question"]


def _opening_prefix(db, business: Business, conversation, lead, route: str) -> str:
    if any(message["direction"] == "out" for message in db.list_messages(job_id=lead["id"])):
        return ""
    parts: list[str] = []
    customer = db.get_customer(lead["customer_id"])
    if route == "returning" and customer:
        parts.append(_returning_greeting(business, customer))
    elif route == "another":
        parts.append("I'll open this as a separate job.")
    ad = _job_ad(lead)
    if ad and ad.get("headline"):
        source_name = ad.get("source") or "Meta"
        campaign = ad.get("campaign")
        bit = f'Thanks for tapping our {source_name} ad — "{ad["headline"]}"'
        if campaign:
            bit += f" ({campaign})"
        parts.append(bit + ".")
    return " ".join(parts).strip()


def _returning_greeting(business: Business, customer: dict) -> str:
    name = customer.get("name") or "there"
    who = f"I'm {business.assistant_name} at {business.name}."
    saved = _saved_address(customer)
    if saved:
        return (
            f"Hi {name}! {who} "
            f"I have {saved} saved — is that still the right address?"
        )
    return f"Hi {name}! {who}"


def _with_opening(opening: str, reply: str) -> str:
    opening = (opening or "").strip()
    reply = (reply or "").strip()
    if not opening:
        return reply
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", opening):
        quoted = re.search(r'"([^"]+)"', sentence)
        if quoted and quoted.group(1) in reply:
            campaign = re.search(r"\(([^)]+)\)", sentence)
            if campaign and campaign.group(1) not in reply:
                kept.append(f"It came from the {campaign.group(1)} campaign.")
            continue
        if sentence and sentence in reply:
            continue
        kept.append(sentence)
    opening = " ".join(kept).strip()
    if not opening or opening in reply:
        return reply
    return f"{opening} {reply}".strip()


def _job_ad(lead: dict | None) -> dict | None:
    if not lead or not (lead.get("ad_id") or lead.get("ad_headline")):
        return None
    return {
        "ad_id": lead.get("ad_id"),
        "campaign": lead.get("campaign"),
        "headline": lead.get("ad_headline"),
        "source": lead.get("ad_source"),
    }


def _facts_of(lead: dict) -> dict:
    return {
        "job_type": lead.get("job_type"),
        "suburb": lead.get("suburb"),
        "urgency": lead.get("urgency"),
        "description": lead.get("description"),
        "preferred_time": lead.get("preferred_time"),
        "contact_name": lead.get("contact_name"),
    }


def _quote_params(lead: dict, quote: dict) -> list[str]:
    who = lead.get("contact_name") or "there"
    label = quote.get("label") or "the job"
    band = f"{int(quote['min_aud'])}-{int(quote['max_aud'])} AUD"
    return [who, label, band]


def _event_summary(business: Business, lead: dict, slot: Slot) -> str:
    label = business.job_label(lead.get("job_type")) or "Job"
    suburb = lead.get("suburb") or "suburb TBC"
    return f"{business.name}: {label} in {suburb} ({slot.label})"


def _event_description(lead: dict, conversation: dict) -> str:
    parts = [
        f"WhatsApp: {conversation.get('wa_id')}",
        f"Name: {lead.get('contact_name') or conversation.get('profile_name') or 'unknown'}",
        f"Urgency: {lead.get('urgency') or 'unknown'}",
    ]
    if lead.get("description"):
        parts.append(lead["description"])
    return "\n".join(parts)


def _notify(notifier, saved, conversation, score_label, handoff_reason, facts) -> None:
    summary = {
        "lead_id": saved["id"],
        "conversation_id": conversation["id"],
        "wa_id": conversation["wa_id"],
        "score": score_label,
        "reason": handoff_reason,
        "job_type": facts.get("job_type"),
        "suburb": facts.get("suburb"),
        "contact_name": facts.get("contact_name"),
    }
    try:
        notifier.handoff(summary)
    except Exception:
        logger.exception("handoff notify failed for lead %s", saved["id"])


def _resolved_ad(inbound: Inbound, business: Business) -> dict:
    found = business.find_ad(inbound.ad_id)
    catalog = _ad_dict(found) if found else {}
    return {
        "ad_id": inbound.ad_id,
        "campaign": catalog.get("campaign"),
        "headline": inbound.ad_headline or catalog.get("headline"),
        "source": catalog.get("source") or inbound.ad_source,
    }


def _ad_dict(ad) -> dict:
    return {
        "ad_id": ad.ad_id,
        "campaign": ad.campaign,
        "headline": ad.headline,
        "source": ad.source,
        "job_type": ad.job_type,
    }


def _new_simulator_wa_id(db: Database) -> str:
    for _ in range(5):
        wa_id = f"sim{secrets.randbelow(900000) + 100000}"
        if db.get_conversation_by_wa(wa_id) is None:
            return wa_id
    raise RuntimeError("could not allocate a simulator number")


def _load_json(raw, fallback):
    if isinstance(raw, (dict, list)):
        return raw
    try:
        value = json.loads(raw or "")
    except json.JSONDecodeError:
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _send_text(gateway: MessagingGateway, to: str, body: str) -> tuple[str | None, str]:
    try:
        return gateway.send_text(to, body), "sent"
    except (NotImplementedError, DeliveryError) as exc:
        logger.warning("delivery skipped for %s: %s", to, exc)
        return None, "skipped"
