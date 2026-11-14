import hashlib
import hmac
import json
import sqlite3

from fastapi.testclient import TestClient

from app.llm import QualificationDraft
from app.main import create_app
from tests.conftest import make_settings
from tests.fakes import MemoryGateway
from tests.test_messaging import _payload

FULL_TEXT = (
    "The safety switch keeps tripping in Newtown. Not urgent. "
    "It trips when the oven is on. My name is Jamie Lee."
)


def test_health_and_home_page(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["llm_provider"] == "mock"
    assert health.json()["trade"] == "electrician"

    page = client.get("/")
    assert page.status_code == 200
    assert "Electrician" in page.text
    assert "ad_fault_01" in page.text
    assert "Northside Electrical" in page.text
    assert "Sample business" in page.text
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/chat.js").status_code == 200


def test_staff_starts_empty(client):
    page = client.get("/staff")
    assert page.status_code == 200
    assert "No jobs yet" in page.text


def test_ad_greeting_is_personal_and_organic_is_not(client):
    ad = client.post(
        "/api/conversations",
        json={"ad_id": "ad_fault_01", "profile_name": "Alex"},
    )
    assert ad.status_code == 200
    body = ad.json()
    opening = body["messages"][0]["body"]
    assert "Alex" in opening
    assert "Emergency electrician — Inner West" in opening
    assert "facebook" in opening.lower()
    assert body["ad"]["campaign"] == "meta_emergency_electrician"
    assert body["lead"]["score"] == "cold"
    assert body["lead"]["job_type"] is None

    organic = client.post("/api/conversations", json={})
    assert "tapping" not in organic.json()["messages"][0]["body"]
    assert organic.json()["ad"] is None


def test_unknown_ad_and_blank_message(client):
    missing = client.post("/api/conversations", json={"ad_id": "not-a-real-ad"})
    assert missing.status_code == 404
    blank = client.post("/api/chat", json={"conversation_id": 1, "text": "   "})
    assert blank.status_code == 422


def test_multi_turn_intake_reaches_hot_and_shows_on_the_desk(client, notifier):
    started = client.post(
        "/api/conversations",
        json={"ad_id": "ad_fault_01", "profile_name": "Alex"},
    ).json()
    conversation_id = started["conversation_id"]

    fault = client.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "text": "The safety switch keeps tripping"},
    )
    assert fault.status_code == 200
    assert fault.json()["lead"]["score"] == "warm"
    assert fault.json()["lead"]["job_type"] == "fault"
    assert fault.json()["lead"]["handoff"] is False
    assert "$" not in fault.json()["reply"]

    suburb = client.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "text": "I'm in Newtown"},
    )
    assert suburb.json()["lead"]["job_type"] == "fault"
    assert suburb.json()["lead"]["suburb"] == "Newtown"
    assert "reply 1, 2, or 3" in suburb.json()["reply"].lower()

    final = client.post(
        "/api/chat",
        json={
            "conversation_id": conversation_id,
            "text": "Not urgent. The switch trips when the oven is on. My name is Alex Rivera",
        },
    )
    lead = final.json()["lead"]
    assert lead["score"] == "hot"
    assert lead["handoff"] is True
    assert lead["handoff_reason"] == "hot_lead"
    assert lead["contact_name"] == "Alex Rivera"
    assert lead["urgency"] == "planned"
    assert "a person from" in final.json()["reply"].lower()
    assert "$" not in final.json()["reply"]
    assert all("$" not in message["body"] for message in final.json()["messages"] if message["direction"] == "out")
    assert lead["quote"]["status"] == "draft"
    assert lead["quote"]["min_aud"] == 220
    assert notifier.events[-1]["reason"] == "hot_lead"

    desk = client.get("/staff")
    assert "Alex Rivera" in desk.text
    assert "hot" in desk.text
    assert "meta_emergency_electrician" in desk.text
    assert "$220" in desk.text

    detail = client.get(f"/staff/{lead['id']}")
    assert detail.status_code == 200
    assert "Newtown" in detail.text
    assert "latest_tool_input" in detail.text
    assert "safety switch" in detail.text
    assert "Approve and send quote" in detail.text
    assert "electrician confirms on site" not in detail.text


def test_outside_service_area_stays_warm(client, notifier):
    conversation_id = client.post("/api/conversations", json={}).json()["conversation_id"]
    result = client.post(
        "/api/chat",
        json={
            "conversation_id": conversation_id,
            "text": (
                "Power point in Bondi. Not urgent. Need a new point in the kitchen "
                "for the fridge. My name is Sam Lee"
            ),
        },
    )
    lead = result.json()["lead"]
    assert lead["score"] == "warm"
    assert lead["suburb"] == "Bondi"
    assert lead["handoff"] is False
    assert "reply 1, 2, or 3" not in result.json()["reply"].lower()
    assert notifier.events == []


def test_asking_for_a_person_hands_off_a_cold_lead(client, notifier):
    conversation_id = client.post("/api/conversations", json={}).json()["conversation_id"]
    result = client.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "text": "Can I talk to a person?"},
    )
    lead = result.json()["lead"]
    assert lead["score"] == "cold"
    assert lead["handoff"] is True
    assert lead["handoff_reason"] == "human_requested"
    assert notifier.events[0]["reason"] == "human_requested"

    again = client.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "text": "Also, a switchboard in Newtown"},
    )
    assert "specialist already has this chat" in again.json()["reply"]
    assert again.json()["lead"]["job_type"] is None


def test_webhook_qualifies_a_click_to_whatsapp_job(client):
    payload = _payload(
        FULL_TEXT,
        referral={
            "source_url": "https://fb.me/sparky",
            "source_id": "ad_fault_01",
            "source_type": "ad",
            "headline": "Emergency electrician — Inner West",
        },
    )
    response = client.post("/webhook", json=payload)
    assert response.status_code == 200
    assert response.json()["processed"] == 1

    page = client.get("/staff")
    assert "Jamie Lee" in page.text
    assert "hot" in page.text
    assert "meta_emergency_electrician" in page.text

    detail = client.get("/staff/1")
    assert "facebook" in detail.text
    assert "oven" in detail.text
    assert "Jamie Lee" in detail.text
    assert "electrician confirms on site" not in detail.text


def test_webhook_duplicate_and_photo(client):
    payload = _payload("Hello from the ad")
    first = client.post("/webhook", json=payload)
    second = client.post("/webhook", json=payload)
    assert first.json()["processed"] == 1
    assert second.json()["duplicates"] == 1
    detail = client.get("/staff/1")
    assert detail.text.count("Hello from the ad") == 1

    image = _payload(None)
    image["entry"][0]["changes"][0]["value"]["messages"] = [
        {"from": "15557650000", "id": "wamid.img", "type": "image", "image": {"id": "9"}}
    ]
    image["entry"][0]["changes"][0]["value"]["contacts"] = [
        {"profile": {"name": "Pat"}, "wa_id": "15557650000"}
    ]
    assert client.post("/webhook", json=image).json()["processed"] == 1
    page = client.get("/staff/2")
    assert "Photo attached (media 9)" in page.text
    assert "media 9" in page.text
    assert "I can read text" not in page.text


def test_customer_can_book_an_offered_slot(client):
    started = client.post("/api/conversations", json={}).json()
    conversation_id = started["conversation_id"]
    client.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "text": "Need a power point in Newtown"},
    )
    booked = client.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "text": "1"},
    )
    assert "You're booked" in booked.json()["reply"]
    assert booked.json()["lead"]["booking"]["status"] == "booked"
    assert booked.json()["lead"]["booking"]["event_id"].startswith("local-")


def test_a_hot_job_can_still_be_booked_from_the_offered_slots(client):
    started = client.post("/api/conversations", json={}).json()
    conversation_id = started["conversation_id"]
    first = client.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "text": FULL_TEXT},
    )
    assert first.json()["lead"]["handoff"] is True
    offered = first.json()["lead"]["booking"]["slots"]
    assert len(offered) == 3
    booked = client.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "text": "2"},
    )
    assert "You're booked" in booked.json()["reply"]
    assert booked.json()["lead"]["booking"]["status"] == "booked"
    assert booked.json()["lead"]["booking"]["label"] == offered[1]["label"]


def test_webhook_verification_and_signature(tmp_path, notifier):
    settings = make_settings(tmp_path, whatsapp_app_secret="secret")
    client = TestClient(create_app(settings=settings, notifier=notifier))

    ok = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "test-token",
            "hub.challenge": "12345",
        },
    )
    assert ok.status_code == 200
    assert ok.text == "12345"
    assert ok.headers["content-type"].startswith("text/plain")

    bad = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "12345"},
    )
    assert bad.status_code == 403

    payload = _payload("A power point in Newtown")
    raw = json.dumps(payload).encode()
    rejected = client.post(
        "/webhook",
        content=raw,
        headers={"content-type": "application/json", "X-Hub-Signature-256": "sha256=nope"},
    )
    assert rejected.status_code == 403
    assert "No jobs yet" in client.get("/staff").text

    digest = hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
    accepted = client.post(
        "/webhook",
        content=raw,
        headers={"content-type": "application/json", "X-Hub-Signature-256": f"sha256={digest}"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["processed"] == 1


def test_status_only_webhook_is_a_no_op(client):
    response = client.post(
        "/webhook",
        json={
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {"statuses": [{"id": "wamid.1", "status": "read"}]},
                        }
                    ]
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["processed"] == 0


def test_scripted_model_null_does_not_wipe_a_known_job(tmp_path, notifier):
    class Scripted:
        def __init__(self):
            self.calls = 0
            self.drafts = [
                QualificationDraft(reply="Fault finding. Which suburb?", job_type="fault"),
                QualificationDraft(reply="Newtown is in the area.", suburb="Newtown"),
                QualificationDraft(reply="I'll bring someone in."),
            ]

        def qualify(self, turn):
            self.calls += 1
            return self.drafts.pop(0)

    scripted = Scripted()
    client = TestClient(
        create_app(settings=make_settings(tmp_path), qualifier=scripted, notifier=notifier)
    )
    conversation_id = client.post("/api/conversations", json={}).json()["conversation_id"]
    client.post("/api/chat", json={"conversation_id": conversation_id, "text": "the switchboard"})
    second = client.post("/api/chat", json={"conversation_id": conversation_id, "text": "Newtown"})
    assert second.json()["lead"]["job_type"] == "fault"
    assert second.json()["lead"]["suburb"] == "Newtown"

    client.post("/api/chat", json={"conversation_id": conversation_id, "text": "talk to a person"})
    assert scripted.calls == 3
    fourth = client.post("/api/chat", json={"conversation_id": conversation_id, "text": "one more thing"})
    assert scripted.calls == 3
    assert "specialist already has this chat" in fourth.json()["reply"]


def test_quoted_price_from_the_model_is_stripped(tmp_path, notifier):
    class Priced:
        def qualify(self, turn):
            return QualificationDraft(
                reply="A switchboard is $1,800 to $4,500. Which suburb?",
                job_type="switchboard",
            )

    client = TestClient(
        create_app(settings=make_settings(tmp_path), qualifier=Priced(), notifier=notifier)
    )
    conversation_id = client.post("/api/conversations", json={}).json()["conversation_id"]
    result = client.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "text": "switchboard please"},
    )
    assert "$" not in result.json()["reply"]
    assert "Which suburb?" in result.json()["reply"]
    assert result.json()["lead"]["quote"]["status"] == "draft"


def test_qualifier_failure_apologises_without_dropping_the_message(tmp_path):
    class Boom:
        def qualify(self, turn):
            raise RuntimeError("model down")

    client = TestClient(create_app(settings=make_settings(tmp_path), qualifier=Boom()))
    conversation_id = client.post("/api/conversations", json={}).json()["conversation_id"]
    result = client.post("/api/chat", json={"conversation_id": conversation_id, "text": "Bali"})
    assert result.status_code == 200
    assert "once more" in result.json()["reply"]
    assert result.json()["lead"]["score"] == "cold"
    assert any(message["body"] == "Bali" for message in result.json()["messages"])


def test_missing_api_key_is_explained(tmp_path):
    settings = make_settings(tmp_path, llm_provider="anthropic", anthropic_api_key="")
    client = TestClient(create_app(settings=settings))
    conversation_id = client.post("/api/conversations", json={}).json()["conversation_id"]
    result = client.post("/api/chat", json={"conversation_id": conversation_id, "text": "Hi"})
    assert "ANTHROPIC_API_KEY" in result.json()["reply"]


def test_from_env_defaults(monkeypatch, tmp_path):
    from app.config import Settings

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "env.db"))
    assert Settings.from_env().llm_provider == "mock"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert Settings.from_env().llm_provider == "anthropic"


def test_env_aliases(monkeypatch, tmp_path):
    from app.config import Settings

    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "env.db"))
    monkeypatch.setenv("PHONE_NUMBER_ID", "123")
    monkeypatch.setenv("VERIFY_TOKEN", "vt")
    monkeypatch.setenv("APP_SECRET", "sec")
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    monkeypatch.delenv("WHATSAPP_VERIFY_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    settings = Settings.from_env()
    assert settings.whatsapp_phone_number_id == "123"
    assert settings.whatsapp_verify_token == "vt"
    assert settings.whatsapp_app_secret == "sec"


def test_quote_is_sent_only_after_staff_approval(tmp_path, notifier):
    gateway = MemoryGateway()
    settings = make_settings(tmp_path)
    client = TestClient(create_app(settings=settings, notifier=notifier, gateway=gateway))
    started = client.post("/api/conversations", json={}).json()
    result = client.post(
        "/api/chat",
        json={"conversation_id": started["conversation_id"], "text": FULL_TEXT},
    )
    lead_id = result.json()["lead"]["id"]
    assert "electrician confirms on site" not in result.json()["reply"]
    before = len(gateway.sent)

    approved = client.post(f"/staff/{lead_id}/quote/approve")
    assert approved.status_code == 200
    assert "electrician confirms on site" in approved.text
    new = gateway.sent[before:]
    assert len(new) == 1
    assert new[0][0] == "text"
    assert "$220" in new[0][2]

    again = client.post(f"/staff/{lead_id}/quote/approve")
    assert len(gateway.sent) == before + 1
    assert "already" not in again.text or "sent" in again.text


def test_quote_outside_24h_uses_the_template_stub(tmp_path, notifier):
    gateway = MemoryGateway()
    settings = make_settings(tmp_path)
    client = TestClient(create_app(settings=settings, notifier=notifier, gateway=gateway))
    started = client.post("/api/conversations", json={}).json()
    result = client.post(
        "/api/chat",
        json={"conversation_id": started["conversation_id"], "text": FULL_TEXT},
    )
    lead_id = result.json()["lead"]["id"]
    with sqlite3.connect(settings.sqlite_path) as conn:
        conn.execute(
            "UPDATE messages SET created_at = ? WHERE direction = 'in'",
            ("2020-01-01 00:00:00 UTC",),
        )
    before = len(gateway.sent)
    page = client.post(f"/staff/{lead_id}/quote/approve")
    assert page.status_code == 200
    new = gateway.sent[before:]
    assert len(new) == 1
    assert new[0][0] == "template"
    assert new[0][2] == "quote_followup"
    assert "[template:quote_followup]" in page.text
    assert "electrician confirms on site" not in page.text


def test_staff_login_gates_the_desk(tmp_path, notifier):
    settings = make_settings(
        tmp_path,
        staff_username="desk",
        staff_password="secret",
        staff_session_secret="session-secret",
    )
    client = TestClient(create_app(settings=settings, notifier=notifier))
    blocked = client.get("/staff", follow_redirects=False)
    assert blocked.status_code == 303
    assert blocked.headers["location"] == "/staff/login"

    wrong = client.post("/staff/login", data={"username": "desk", "password": "nope"})
    assert wrong.status_code == 401

    signed_in = client.post(
        "/staff/login",
        data={"username": "desk", "password": "secret"},
        follow_redirects=False,
    )
    assert signed_in.status_code == 303
    desk = client.get("/staff")
    assert desk.status_code == 200
    assert "No jobs yet" in desk.text

    stranger = TestClient(create_app(settings=settings, notifier=notifier))
    denied = stranger.post("/staff/1/quote/approve")
    assert denied.status_code == 401

