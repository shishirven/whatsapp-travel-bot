"""HTTP surface: simulated WhatsApp chat, staff desk, and the Cloud API webhook."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from app.auth import COOKIE, auth_enabled, credentials_match, issue_session, read_session
from app.booking import build_calendar
from app.business import load_business
from app.config import Settings
from app.customers_io import ImportResult, import_csv_text
from app.db import Database
from app.llm import AnthropicQualifier, MissingKeyQualifier, RuleBasedQualifier
from app.media import (
    GraphMediaDownloader,
    OpenAIWhisperTranscriber,
    UnconfiguredMediaDownloader,
    UnconfiguredTranscriber,
)
from app.messaging import (
    Inbound,
    SimulatedGateway,
    WhatsAppCloudGateway,
    parse_webhook,
    verify_meta_signature,
)
from app.notify import LogNotifier
from app.routing import JOB_STATUSES, build_intent
from app.service import (
    HANDOFF_LABELS,
    QuoteNotReady,
    UnknownAd,
    approve_quote,
    conversation_snapshot,
    handle_incoming,
    lead_out,
    start_simulated_chat,
)

logger = logging.getLogger("tradie")
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


def build_qualifier(settings: Settings, business):
    if settings.llm_provider == "mock":
        return RuleBasedQualifier(business)
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


def build_media(settings: Settings):
    if not settings.whatsapp_token:
        return UnconfiguredMediaDownloader()
    return GraphMediaDownloader(settings.whatsapp_token, settings.graph_version)


def build_transcriber(settings: Settings):
    if not settings.openai_api_key:
        return UnconfiguredTranscriber()
    return OpenAIWhisperTranscriber(settings.openai_api_key, settings.whisper_model)


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
    business=None,
    calendar=None,
    media_downloader=None,
    transcriber=None,
    now=None,
    intent=None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    clock = now or (lambda: datetime.now(timezone.utc))
    business = business or load_business(settings.business_config_path or None)
    db = Database(settings.sqlite_path, clock=clock)
    qualifier = qualifier if qualifier is not None else build_qualifier(settings, business)
    gateway = gateway if gateway is not None else build_gateway(settings)
    notifier = notifier if notifier is not None else LogNotifier()
    calendar = calendar if calendar is not None else build_calendar(settings, db)
    media_downloader = media_downloader if media_downloader is not None else build_media(settings)
    transcriber = transcriber if transcriber is not None else build_transcriber(settings)
    intent = intent if intent is not None else build_intent(
        settings.llm_provider, settings.anthropic_api_key, settings.anthropic_model
    )

    app = FastAPI(title=f"{business.name} jobs", docs_url="/docs")
    app.mount("/static", StaticFiles(directory=str(_ROOT / "static")), name="static")
    app.state.settings = settings
    app.state.db = db
    app.state.business = business
    app.state.calendar = calendar
    logger.info(
        "ready llm=%s messaging=%s db=%s trade=%s staff_auth=%s",
        settings.llm_provider,
        settings.messaging_provider,
        settings.sqlite_path,
        business.trade,
        "on" if settings.staff_auth_enabled else "off",
    )
    if not settings.staff_auth_enabled:
        logger.warning("STAFF_USERNAME or STAFF_PASSWORD is empty; /staff has no login")
    if not settings.whatsapp_app_secret:
        logger.warning("APP_SECRET is empty; set it before exposing the webhook")

    def moment() -> datetime:
        current = clock()
        if current.tzinfo is None:
            return current.replace(tzinfo=timezone.utc)
        return current

    def staff_guard(request: Request):
        if not auth_enabled(settings):
            return None
        if read_session(settings, request.cookies.get(COOKIE)):
            return None
        if request.method == "GET":
            return RedirectResponse("/staff/login", status_code=303)
        raise HTTPException(status_code=401, detail="Login required")

    @app.get("/api/health")
    def health():
        status = llm_status(settings, qualifier)
        return {
            "status": "ok",
            "llm_provider": status["provider"],
            "llm_label": status["label"],
            "messaging_provider": settings.messaging_provider,
            "business": business.name,
            "trade": business.trade,
        }

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return templates.TemplateResponse(
            request,
            "chat.html",
            {
                "title": "Simulator",
                "active": "chat",
                "business": business,
                "ads": business.ads,
                "banner": business.banner,
                "llm": llm_status(settings, qualifier),
            },
        )

    @app.get("/staff/login", response_class=HTMLResponse)
    def login_form(request: Request, error: str | None = None):
        if not auth_enabled(settings):
            return RedirectResponse("/staff", status_code=303)
        if read_session(settings, request.cookies.get(COOKIE)):
            return RedirectResponse("/staff", status_code=303)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "title": "Staff login",
                "active": "staff",
                "business": business,
                "error": error,
            },
            status_code=401 if error else 200,
        )

    @app.post("/staff/login")
    def login(request: Request, username: str = Form(""), password: str = Form("")):
        if not auth_enabled(settings):
            return RedirectResponse("/staff", status_code=303)
        if not credentials_match(settings, username.strip(), password):
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "title": "Staff login",
                    "active": "staff",
                    "business": business,
                    "error": "Those details don't match.",
                },
                status_code=401,
            )
        response = RedirectResponse("/staff", status_code=303)
        response.set_cookie(
            COOKIE,
            issue_session(settings, username.strip()),
            httponly=True,
            samesite="lax",
            max_age=12 * 60 * 60,
            secure=_cookie_secure(request),
        )
        return response

    @app.post("/staff/logout")
    def logout():
        response = RedirectResponse("/staff/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    @app.get("/staff", response_class=HTMLResponse)
    def staff(request: Request):
        blocked = staff_guard(request)
        if blocked is not None:
            return blocked
        jobs = [lead_out(row, business) for row in db.list_leads()]
        return templates.TemplateResponse(
            request,
            "staff.html",
            {
                "title": "Jobs",
                "active": "staff",
            "business": business,
            "leads": jobs,
            "banner": business.banner,
            "auth_enabled": auth_enabled(settings),
        },
    )

    def customer_page(request: Request, import_result: ImportResult | None = None):
        customers = []
        for row in db.list_customers():
            numbers = db.list_numbers(row["id"])
            customers.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "notes": row["notes"],
                    "trusted": bool(row["trusted"]),
                    "addresses": _json_list(row.get("addresses_json")),
                    "numbers": [item["wa_id"] for item in numbers],
                    "job_count": len(db.list_jobs_for_customer(row["id"])),
                }
            )
        return templates.TemplateResponse(
            request,
            "customers.html",
            {
                "title": "Customers",
                "active": "customers",
                "business": business,
                "customers": customers,
                "import_result": import_result,
                "auth_enabled": auth_enabled(settings),
            },
        )

    @app.get("/staff/customers", response_class=HTMLResponse)
    def staff_customers(request: Request):
        blocked = staff_guard(request)
        if blocked is not None:
            return blocked
        return customer_page(request)

    @app.post("/staff/customers/import", response_class=HTMLResponse)
    async def staff_import_customers(request: Request, file: UploadFile = File(...)):
        blocked = staff_guard(request)
        if blocked is not None:
            return blocked
        raw = await file.read()
        if not raw:
            result = ImportResult(errors=["Choose a CSV file."])
        else:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                result = ImportResult(errors=["CSV must be UTF-8."])
            else:
                try:
                    result = import_csv_text(db, text)
                except ValueError as exc:
                    result = ImportResult(errors=[str(exc)])
        return customer_page(request, result)

    @app.get("/staff/customers/{customer_id}", response_class=HTMLResponse)
    def staff_customer(request: Request, customer_id: int):
        blocked = staff_guard(request)
        if blocked is not None:
            return blocked
        customer = db.get_customer(customer_id)
        if customer is None:
            raise HTTPException(status_code=404, detail="Unknown customer")
        jobs = [lead_out(row, business) for row in db.list_jobs_for_customer(customer_id)]
        return templates.TemplateResponse(
            request,
            "customer.html",
            {
                "title": customer.get("name") or f"Customer {customer_id}",
                "active": "customers",
                "business": business,
                "customer": customer,
                "addresses": _json_list(customer.get("addresses_json")),
                "numbers": db.list_numbers(customer_id),
                "jobs": jobs,
                "auth_enabled": auth_enabled(settings),
            },
        )

    @app.get("/staff/{lead_id}", response_class=HTMLResponse)
    def staff_lead(request: Request, lead_id: int, send: str | None = None):
        blocked = staff_guard(request)
        if blocked is not None:
            return blocked
        lead = db.get_lead(lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="Unknown job")
        view = lead_out(lead, business)
        candidate = None
        candidate_addresses: list[str] = []
        if lead.get("possible_customer_id"):
            candidate = db.get_customer(lead["possible_customer_id"])
            if candidate:
                candidate_addresses = _json_list(candidate.get("addresses_json"))
        other_jobs = [
            item
            for item in (
                lead_out(row, business) for row in db.list_jobs_for_customer(lead["customer_id"])
            )
            if item["id"] != lead_id
        ]
        link_targets = [
            row for row in db.list_customers() if row["id"] != lead["customer_id"]
        ]
        return templates.TemplateResponse(
            request,
            "lead.html",
            {
                "title": view.get("contact_name") or f"Job {lead_id}",
                "active": "staff",
                "business": business,
                "lead": view,
                "messages": db.list_messages(lead["conversation_id"], job_id=lead_id),
                "extracted": _pretty_json(lead.get("extracted_json")),
                "handoff_label": HANDOFF_LABELS.get(lead.get("handoff_reason") or "", ""),
                "send_error": send,
                "auth_enabled": auth_enabled(settings),
                "candidate": candidate,
                "candidate_addresses": candidate_addresses,
                "other_jobs": other_jobs,
                "link_targets": link_targets,
                "job_statuses": JOB_STATUSES,
            },
        )

    @app.get("/staff/media/{lead_id}/{media_id}")
    def staff_media(request: Request, lead_id: int, media_id: str):
        blocked = staff_guard(request)
        if blocked is not None:
            return blocked
        lead = db.get_lead(lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="Unknown job")
        path = _saved_media_path(lead, media_id, Path(db.path).parent / "media")
        if path is None:
            raise HTTPException(status_code=404, detail="Media not on this job")
        return FileResponse(path)

    @app.post("/staff/{lead_id}/quote/approve")
    def staff_approve_quote(request: Request, lead_id: int):
        blocked = staff_guard(request)
        if blocked is not None:
            return blocked
        try:
            result = approve_quote(
                db,
                gateway,
                lead_id,
                business=business,
                now=moment(),
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown job")
        except QuoteNotReady as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        target = f"/staff/{lead_id}"
        if not result.get("sent"):
            target = f"{target}?send={result.get('reason') or 'skipped'}"
        return RedirectResponse(target, status_code=303)

    @app.post("/staff/{lead_id}/link")
    def staff_link_number(request: Request, lead_id: int, customer_id: int = Form(...)):
        blocked = staff_guard(request)
        if blocked is not None:
            return blocked
        try:
            linked = db.link_number(lead_id, customer_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown job or customer")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return RedirectResponse(f"/staff/customers/{linked['customer_id']}", status_code=303)

    @app.post("/staff/{lead_id}/split")
    def staff_split_job(request: Request, lead_id: int, message_id: int = Form(...)):
        blocked = staff_guard(request)
        if blocked is not None:
            return blocked
        try:
            created = db.split_job(lead_id, [message_id])
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown job")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return RedirectResponse(f"/staff/{created['id']}", status_code=303)

    @app.post("/staff/{lead_id}/status")
    def staff_set_status(request: Request, lead_id: int, status: str = Form(...)):
        blocked = staff_guard(request)
        if blocked is not None:
            return blocked
        try:
            db.set_job_status(lead_id, status)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown job")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return RedirectResponse(f"/staff/{lead_id}", status_code=303)

    @app.post("/staff/messages/{message_id}/reassign")
    def staff_reassign_message(
        request: Request, message_id: int, job_id: int = Form(...)
    ):
        blocked = staff_guard(request)
        if blocked is not None:
            return blocked
        try:
            job = db.reassign_message(message_id, job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown message or job")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return RedirectResponse(f"/staff/{job['id']}", status_code=303)

    @app.post("/api/conversations")
    def start_conversation(body: StartIn):
        try:
            return start_simulated_chat(
                db,
                gateway,
                ad_id=body.ad_id,
                profile_name=body.profile_name,
                business=business,
                now=moment(),
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
        return handle_incoming(
            db,
            qualifier,
            gateway,
            notifier,
            inbound,
            business=business,
            calendar=calendar,
            media=media_downloader,
            transcriber=transcriber,
            now=moment(),
            intent=intent,
        )

    @app.get("/api/conversations/{conversation_id}")
    def read_conversation(conversation_id: int):
        try:
            return conversation_snapshot(db, conversation_id, business)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown conversation")

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
            result = handle_incoming(
                db,
                qualifier,
                gateway,
                notifier,
                inbound,
                business=business,
                calendar=calendar,
                media=media_downloader,
                transcriber=transcriber,
                now=moment(),
                intent=intent,
            )
            if result.get("duplicate"):
                duplicates += 1
            else:
                processed += 1
        return {"status": "ok", "processed": processed, "duplicates": duplicates}

    return app


def _cookie_secure(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or forwarded.lower() == "https"


def _saved_media_path(lead: dict, media_id: str, root: Path) -> Path | None:
    root = root.resolve()
    blobs = []
    for raw in (lead.get("photos_json"), lead.get("voice_json")):
        try:
            parsed = json.loads(raw or "[]")
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            blobs.extend(parsed)
    for item in blobs:
        if not isinstance(item, dict) or item.get("media_id") != media_id:
            continue
        stored = item.get("path")
        if not stored:
            continue
        path = Path(stored).resolve()
        if path == root or root in path.parents:
            if path.is_file():
                return path
    return None


def _json_list(raw) -> list:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _pretty_json(raw: str | None) -> str:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return raw or "{}"
    return json.dumps(data, indent=2)


app = create_app()
