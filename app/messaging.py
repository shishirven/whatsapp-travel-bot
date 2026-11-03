"""Outbound messaging and the inbound WhatsApp webhook shape.

`MessagingGateway` is the seam. The simulator mints an id and the conversation
service stores the text. `WhatsAppCloudGateway` builds the real Cloud API
request and sends it only when credentials exist — the Meta app setup is still
a TODO, listed on the class.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger("horizon.messaging")

_signature_warning_sent = False


class DeliveryError(RuntimeError):
    """The provider accepted the call shape but did not accept the message."""


class MessagingGateway(Protocol):
    def send_text(self, to: str, body: str) -> str:
        """Deliver one text message. Return the provider message id."""


@dataclass
class Inbound:
    """One traveller message, whether it came from the simulator or Meta."""

    wa_id: str
    text: str | None
    message_type: str
    provider_message_id: str | None = None
    profile_name: str | None = None
    ad_id: str | None = None
    ad_headline: str | None = None
    ad_source: str | None = None
    source: str = "whatsapp"


class SimulatedGateway:
    """Local stand-in. Nothing is sent anywhere; the transcript lives in SQLite."""

    def send_text(self, to: str, body: str) -> str:
        return f"sim_{uuid.uuid4().hex[:16]}"


class WhatsAppCloudGateway:
    """Outbound adapter for the WhatsApp Cloud API.

    The JSON body matches the Graph API. Delivery stays off until the env vars
    are set. Before `MESSAGING_PROVIDER=whatsapp` will reach a phone:

    TODO: Create a Meta app, add WhatsApp, and copy the phone number id into
          WHATSAPP_PHONE_NUMBER_ID.
    TODO: Put a token in WHATSAPP_TOKEN. A temporary dashboard token is enough
          to try; a system-user token is what you want to keep.
    TODO: Expose this app on public HTTPS, subscribe the app to the `messages`
          field, and set WHATSAPP_VERIFY_TOKEN to the same string.
    TODO: Set WHATSAPP_APP_SECRET so POST /webhook checks X-Hub-Signature-256.
    TODO: Outside the 24-hour customer-care window, send an approved template
          instead of free-form text. This method only sends session text.
    TODO: Ack the webhook immediately and send from a queue. This demo answers
          inside the request, which is fine at one-agency volume.
    """

    def __init__(self, token: str, phone_number_id: str, version: str = "v21.0") -> None:
        self.token = token
        self.phone_number_id = phone_number_id
        self.version = version

    def messages_url(self) -> str:
        # TODO: confirm `version` against the version you subscribed in the dashboard.
        return (
            f"https://graph.facebook.com/{self.version}/{self.phone_number_id}/messages"
        )

    def build_payload(self, to: str, body: str) -> dict:
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": body},
        }

    def send_text(self, to: str, body: str) -> str:
        payload = self.build_payload(to, body)
        if not self.token or not self.phone_number_id:
            raise NotImplementedError(
                "WhatsApp send is not configured. "
                "TODO: set WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID. "
                f"Unsent text for {to} ({len(body)} chars) would POST to {self.messages_url()}."
            )
        try:
            response = httpx.post(
                self.messages_url(),
                headers={"Authorization": f"Bearer {self.token}"},
                json=payload,
                timeout=15,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise DeliveryError(str(exc)) from exc
        data = response.json()
        try:
            return data["messages"][0]["id"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DeliveryError(f"unexpected WhatsApp response: {data!r}") from exc


def verify_meta_signature(body: bytes, header: str | None, secret: str | None) -> bool:
    """Check X-Hub-Signature-256. An empty secret means local dev: allow the call."""
    global _signature_warning_sent
    if not secret:
        if not _signature_warning_sent:
            logger.warning("WHATSAPP_APP_SECRET is empty; webhook signature check is off")
            _signature_warning_sent = True
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len("sha256="):], expected)


def parse_webhook(payload: dict) -> list[Inbound]:
    """Pull traveller messages out of a WhatsApp Cloud API webhook body.

    Status callbacks (`delivered`, `read`) and other products return an empty
    list. Referral fields are the click-to-WhatsApp ad context.
    """
    if not isinstance(payload, dict):
        return []
    obj = payload.get("object")
    if obj not in (None, "whatsapp_business_account"):
        return []

    found: list[Inbound] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = _profile_names(value.get("contacts") or [])
            for message in value.get("messages") or []:
                wa_id = message.get("from")
                if not wa_id:
                    continue
                referral = message.get("referral") or {}
                text = _message_text(message)
                found.append(
                    Inbound(
                        wa_id=str(wa_id),
                        text=text,
                        message_type=str(message.get("type") or "unknown"),
                        provider_message_id=message.get("id"),
                        profile_name=names.get(str(wa_id)),
                        ad_id=_blank_to_none(referral.get("source_id")),
                        ad_headline=_blank_to_none(referral.get("headline")),
                        ad_source=_ad_source(referral),
                        source="whatsapp",
                    )
                )
    return found


def _profile_names(contacts: list) -> dict[str, str | None]:
    names: dict[str, str | None] = {}
    for contact in contacts:
        if not isinstance(contact, dict):
            continue
        wa_id = contact.get("wa_id")
        if not wa_id:
            continue
        profile = contact.get("profile") or {}
        names[str(wa_id)] = _blank_to_none(profile.get("name"))
    return names


def _message_text(message: dict) -> str | None:
    kind = message.get("type")
    if kind == "text":
        return _blank_to_none((message.get("text") or {}).get("body"))
    if kind == "button":
        return _blank_to_none((message.get("button") or {}).get("text"))
    if kind == "interactive":
        interactive = message.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        return _blank_to_none(reply.get("title"))
    return None


def _ad_source(referral: dict) -> str | None:
    if not referral:
        return None
    url = str(referral.get("source_url") or "").lower()
    if "instagram" in url:
        return "instagram"
    if "facebook" in url or "fb.me" in url or "fb.com" in url:
        return "facebook"
    source_type = _blank_to_none(referral.get("source_type"))
    return source_type


def _blank_to_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
