"""HTTP surface: simulated WhatsApp chat, staff desk, and the Cloud API webhook."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from app.config import Settings
from app.db import Database
from app.knowledge import SAMPLE_ADS, SAMPLE_BANNER
from app.llm import AnthropicQualifier, MissingKeyQualifier, RuleBasedQualifier
from app.messaging import (
    Inbound,
    SimulatedGateway,
    WhatsAppCloudGateway,
    parse_webhook,
    verify_meta_signature,
)
from app.notify import LogNotifier
from app.service import (
    HANDOFF_LABELS,
    UnknownAd,
    handle_incoming,
    start_simulated_chat,
)

logger = logging.getLogger("horizon")
_ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_ROOT / "templates"))


class StartIn(BaseModel):
    ad_id: str | None = None
    profile_name: str | None = Field(default=None, max_length=80)

    @field_validator("ad_id", "profile_name", mode="before")
    @classmethod
    def blank_to_none(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class ChatIn(BaseModel):
    conversation_id: int
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message is empty")
        return value


def build_qualifier(settings: Settings):
    if settings.llm_provider == "mock":
        return RuleBasedQualifier()
    if not settings.anthropic_api_key:
        return MissingKeyQualifier()
    return AnthropicQualifier(settings.anthropic_api_key, settings.anthropic_model)


def build_gateway(settings: Settings):
    if settings.messaging_provider == "simulated":
        return SimulatedGateway()
    return WhatsAppCloudGateway(
        token=settings.whatsapp_token,
        phone_number_id=settings.whatsapp_phone_number_id,
        version=settings.graph_version,
    )


def llm_status(settings: Settings, qualifier) -> dict:
    if isinstance(qualifier, AnthropicQualifier):
        return {
            "provider": "anthropic",
            "model": settings.anthropic_model,
            "label": f"Claude · {settings.anthropic_model}",
        }
    if isinstance(qualifier, MissingKeyQualifier):
        return {
            "provider": "unconfigured",
            "model": settings.anthropic_model,
            "label": "Claude is selected, but ANTHROPIC_API_KEY is empty",
        }
    return {
        "provider": "mock",
        "model": None,
        "label": "Offline practice qualifier (no API key)",
    }


def create_app(
    settings: Settings | None = None,
    qualifier=None,
    gateway=None,
    notifier=None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.sqlite_path)
    qualifier = qualifier if qualifier is not None else build_qualifier(settings)
    gateway = gateway if gateway is not None else build_gateway(settings)
    notifier = notifier if notifier is not None else LogNotifier()

    app = FastAPI(title="Horizon Sample Travel leads", docs_url="/docs")
    app.mount("/static", StaticFiles(directory=str(_ROOT / "static")), name="static")
    app.state.settings = settings
    app.state.db = db
    app.state.qualifier = qualifier
    app.state.gateway = gateway
    app.state.notifier = notifier
    logger.info(
        "ready llm=%s messaging=%s db=%s",
        settings.llm_provider,
        settings.messaging_provider,
        settings.sqlite_path,
    )

    @app.get("/api/health")
    def health():
        status = llm_status(settings, qualifier)
        return {
            "status": "ok",
            "llm_provider": status["provider"],
            "llm_label": status["label"],
            "messaging_provider": settings.messaging_provider,
        }

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return templates.TemplateResponse(
            request,
            "chat.html",
            {
                "title": "Simulator",
                "active": "chat",
                "ads": SAMPLE_ADS,
                "banner": SAMPLE_BANNER,
                "llm": llm_status(settings, qualifier),
            },
        )

    @app.get("/staff", response_class=HTMLResponse)
    def staff(request: Request):
        return templates.TemplateResponse(
            request,
            "staff.html",
            {
                "title": "Leads",
                "active": "staff",
                "leads": db.list_leads(),
                "banner": SAMPLE_BANNER,
            },
        )

    @app.get("/staff/{lead_id}", response_class=HTMLResponse)
    def staff_lead(request: Request, lead_id: int):
        lead = db.get_lead(lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="Unknown lead")
        return templates.TemplateResponse(
            request,
            "lead.html",
            {
                "title": f"Lead {lead_id}",
                "active": "staff",
                "lead": lead,
                "messages": db.list_messages(lead["conversation_id"]),
                "extracted": _pretty_json(lead.get("extracted_json")),
                "handoff_label": HANDOFF_LABELS.get(lead.get("handoff_reason") or "", ""),
            },
        )

    @app.post("/api/conversations")
    def start_conversation(body: StartIn):
        try:
            return start_simulated_chat(
                db,
                gateway,
                ad_id=body.ad_id,
                profile_name=body.profile_name,
            )
        except UnknownAd:
            raise HTTPException(status_code=404, detail="Unknown sample ad. Pick one from the list.")

    @app.post("/api/chat")
    def chat(body: ChatIn):
        conversation = db.get_conversation(body.conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Unknown conversation")
        inbound = Inbound(
            wa_id=conversation["wa_id"],
            text=body.text,
            message_type="text",
            profile_name=conversation.get("profile_name"),
            source=conversation.get("source") or "simulator",
        )
        return handle_incoming(db, qualifier, gateway, notifier, inbound)

    @app.get("/webhook")
    def verify_webhook(
        hub_mode: str | None = Query(None, alias="hub.mode"),
        hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
        hub_challenge: str | None = Query(None, alias="hub.challenge"),
    ):
        # Meta's handshake. The response body must be the challenge, not JSON.
        if (
            hub_mode == "subscribe"
            and settings.whatsapp_verify_token
            and hub_verify_token == settings.whatsapp_verify_token
            and hub_challenge is not None
        ):
            return PlainTextResponse(hub_challenge)
        raise HTTPException(status_code=403, detail="Webhook verification failed")

    @app.post("/webhook")
    async def webhook(request: Request):
        body = await request.body()
        signature = request.headers.get("X-Hub-Signature-256")
        if not verify_meta_signature(body, signature, settings.whatsapp_app_secret):
            logger.warning("rejected webhook: bad signature")
            raise HTTPException(status_code=403, detail="Invalid signature")
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Expected a JSON object")

        processed = 0
        duplicates = 0
        for inbound in parse_webhook(payload):
            result = handle_incoming(db, qualifier, gateway, notifier, inbound)
            if result.get("duplicate"):
                duplicates += 1
            else:
                processed += 1
        return {"status": "ok", "processed": processed, "duplicates": duplicates}

    return app


def _pretty_json(raw: str | None) -> str:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return raw or "{}"
    return json.dumps(data, indent=2)


app = create_app()
