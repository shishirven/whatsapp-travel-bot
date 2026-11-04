import hashlib
import hmac

import httpx

from app.messaging import WhatsAppCloudGateway, parse_webhook, verify_meta_signature


def test_signature_round_trip_and_dev_bypass():
    body = b'{"ok": true}'
    digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_meta_signature(body, f"sha256={digest}", "secret")
    assert not verify_meta_signature(body, "sha256=deadbeef", "secret")
    assert not verify_meta_signature(body, None, "secret")
    assert verify_meta_signature(body, None, "")


def test_parse_webhook_reads_text_profile_and_referral():
    payload = _payload(
        "Is the Bali week still open?",
        referral={
            "source_url": "https://www.instagram.com/p/example",
            "source_id": "ad_kenya_safari_02",
            "source_type": "ad",
            "headline": "Kenya safari week from $2,450",
        },
    )
    messages = parse_webhook(payload)
    assert len(messages) == 1
    inbound = messages[0]
    assert inbound.wa_id == "15551230000"
    assert inbound.text == "Is the Bali week still open?"
    assert inbound.profile_name == "Jamie"
    assert inbound.ad_id == "ad_kenya_safari_02"
    assert inbound.ad_headline == "Kenya safari week from $2,450"
    assert inbound.ad_source == "instagram"
    assert inbound.provider_message_id == "wamid.1"


def test_parse_ignores_status_callbacks_and_other_products():
    assert parse_webhook({"object": "page", "entry": []}) == []
    statuses = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {"statuses": [{"id": "wamid.1", "status": "delivered"}]},
                    }
                ]
            }
        ],
    }
    assert parse_webhook(statuses) == []


def test_facebook_short_link_counts_as_facebook():
    payload = _payload(
        "Hi",
        referral={"source_url": "https://fb.me/abc", "source_id": "999", "headline": "A trip"},
    )
    assert parse_webhook(payload)[0].ad_source == "facebook"


def test_non_text_message_has_no_body():
    payload = _payload(None)
    payload["entry"][0]["changes"][0]["value"]["messages"] = [
        {"from": "15551230000", "id": "wamid.img", "type": "image", "image": {"id": "1"}}
    ]
    inbound = parse_webhook(payload)[0]
    assert inbound.text is None
    assert inbound.message_type == "image"


def test_gateway_builds_the_cloud_api_body_and_posts_it(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"messages": [{"id": "wamid.out"}]}

    def fake_post(url, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    gateway = WhatsAppCloudGateway(token="token", phone_number_id="PHONE", version="v21.0")
    assert gateway.send_text("15551230000", "Hello") == "wamid.out"
    assert captured["url"] == "https://graph.facebook.com/v21.0/PHONE/messages"
    assert captured["headers"]["Authorization"] == "Bearer token"
    assert captured["json"]["messaging_product"] == "whatsapp"
    assert captured["json"]["text"]["body"] == "Hello"
    assert captured["json"]["to"] == "15551230000"


def test_gateway_without_credentials_is_an_explicit_stub():
    gateway = WhatsAppCloudGateway(token="", phone_number_id="", version="v21.0")
    try:
        gateway.send_text("1555", "Hello")
    except NotImplementedError as exc:
        assert "WHATSAPP_TOKEN" in str(exc)
    else:
        raise AssertionError("expected NotImplementedError")


def _payload(body, referral=None):
    message = {
        "from": "15551230000",
        "id": "wamid.1",
        "timestamp": "1762000000",
        "type": "text",
        "text": {"body": body or ""},
    }
    if referral:
        message["referral"] = referral
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550001111",
                                "phone_number_id": "PHONE_ID",
                            },
                            "contacts": [{"profile": {"name": "Jamie"}, "wa_id": "15551230000"}],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }
