import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.llm import QualificationDraft
from app.main import create_app
from tests.conftest import make_settings
from tests.test_messaging import _payload


FULL_TEXT = (
    "We're 4 people going to Bali June 12-19 2026. Budget is 4000. My name is Jamie Lee."
)


def test_health_and_home_page(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["llm_provider"] == "mock"

    page = client.get("/")
    assert page.status_code == 200
    assert "Fictional demo" in page.text
    assert "ad_bali_summer_01" in page.text
    assert "SAMPLE DATA" in page.text
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/chat.js").status_code == 200


def test_staff_starts_empty(client):
    page = client.get("/staff")
    assert page.status_code == 200
    assert "No leads yet" in page.text


def test_ad_greeting_is_personal_and_organic_is_not(client):
    ad = client.post(
        "/api/conversations",
        json={"ad_id": "ad_bali_summer_01", "profile_name": "Alex"},
    )
    assert ad.status_code == 200
    body = ad.json()
    opening = body["messages"][0]["body"]
    assert "Alex" in opening
    assert "7 days in Bali from $1,299" in opening
    assert "facebook" in opening.lower()
    assert body["ad"]["campaign"] == "meta_bali_summer_2026"
    assert body["lead"]["score"] == "cold"

    organic = client.post("/api/conversations", json={})
    assert "tapping" not in organic.json()["messages"][0]["body"]
    assert organic.json()["ad"] is None


def test_unknown_ad_and_blank_message(client):
    missing = client.post("/api/conversations", json={"ad_id": "not-a-real-ad"})
    assert missing.status_code == 404
    blank = client.post("/api/chat", json={"conversation_id": 1, "text": "   "})
    assert blank.status_code == 422


def test_multi_turn_qualification_reaches_hot_and_shows_on_the_desk(client, notifier):
    started = client.post(
        "/api/conversations",
        json={"ad_id": "ad_bali_summer_01", "profile_name": "Alex"},
    ).json()
    conversation_id = started["conversation_id"]

    bali = client.post("/api/chat", json={"conversation_id": conversation_id, "text": "Bali please"})
    assert bali.status_code == 200
    assert bali.json()["lead"]["score"] == "warm"
    assert bali.json()["lead"]["destination"] == "Bali, Indonesia"
    assert bali.json()["lead"]["handoff"] is False

    dates = client.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "text": "June 12-19 2026"},
    )
    assert dates.json()["lead"]["destination"] == "Bali, Indonesia"
    assert dates.json()["lead"]["travel_dates"] == "June 12-19 2026"

    final = client.post(
        "/api/chat",
        json={
            "conversation_id": conversation_id,
            "text": "4 people, budget is 4000, my name is Alex Rivera",
        },
    )
    lead = final.json()["lead"]
    assert lead["score"] == "hot"
    assert lead["handoff"] is True
    assert lead["handoff_reason"] == "hot_lead"
    assert lead["group_size"] == 4
    assert lead["budget_usd"] == 4000
    assert lead["contact_name"] == "Alex Rivera"
    assert "a person from" in final.json()["reply"].lower()
    assert notifier.events[-1]["reason"] == "hot_lead"

    desk = client.get("/staff")
    assert "Alex Rivera" in desk.text
    assert "hot" in desk.text
    assert "meta_bali_summer_2026" in desk.text

    detail = client.get(f"/staff/{lead['id']}")
    assert detail.status_code == 200
    assert "June 12-19 2026" in detail.text
    assert "latest_tool_input" in detail.text
    assert "Bali please" in detail.text


def test_low_budget_stays_warm(client, notifier):
    conversation_id = client.post("/api/conversations", json={}).json()["conversation_id"]
    result = client.post(
        "/api/chat",
        json={
            "conversation_id": conversation_id,
            "text": "Bali, June 12-19 2026, 2 people, budget is 400, my name is Sam",
        },
    )
    lead = result.json()["lead"]
    assert lead["score"] == "warm"
    assert lead["handoff"] is False
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
        json={"conversation_id": conversation_id, "text": "Also, Kyoto in May"},
    )
    assert "specialist already has this chat" in again.json()["reply"]
    assert again.json()["lead"]["destination"] is None


def test_webhook_qualifies_a_click_to_whatsapp_lead(client):
    payload = _payload(
        FULL_TEXT,
        referral={
            "source_url": "https://fb.me/bali",
            "source_id": "ad_bali_summer_01",
            "source_type": "ad",
            "headline": "7 days in Bali from $1,299",
        },
    )
    response = client.post("/webhook", json=payload)
    assert response.status_code == 200
    assert response.json()["processed"] == 1

    page = client.get("/staff")
    assert "Jamie Lee" in page.text
    assert "hot" in page.text
    assert "meta_bali_summer_2026" in page.text

    detail_href = "/staff/1"
    detail = client.get(detail_href)
    assert "facebook" in detail.text
    # The apostrophe in "We're" is escaped in HTML; check the rest of the turn.
    assert "Budget is 4000" in detail.text
    assert "Jamie Lee" in detail.text


def test_webhook_duplicate_and_non_text(client):
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
    assert "[image message]" in client.get("/staff/2").text
    assert "I can read text" in client.get("/staff/2").text


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

    payload = _payload("Bali in June, 2 people")
    raw = json.dumps(payload).encode()
    rejected = client.post(
        "/webhook",
        content=raw,
        headers={"content-type": "application/json", "X-Hub-Signature-256": "sha256=nope"},
    )
    assert rejected.status_code == 403
    assert "No leads yet" in client.get("/staff").text

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


def test_scripted_model_null_does_not_wipe_a_known_destination(tmp_path, notifier):
    class Scripted:
        def __init__(self):
            self.calls = 0
            self.drafts = [
                QualificationDraft(reply="Bali is a great sample week. What dates?", destination="Bali, Indonesia"),
                QualificationDraft(reply="June is open on the sample sheet.", travel_dates="June 2026"),
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
    client.post("/api/chat", json={"conversation_id": conversation_id, "text": "Bali"})
    second = client.post("/api/chat", json={"conversation_id": conversation_id, "text": "June"})
    assert second.json()["lead"]["destination"] == "Bali, Indonesia"
    assert second.json()["lead"]["travel_dates"] == "June 2026"

    client.post("/api/chat", json={"conversation_id": conversation_id, "text": "talk to a person"})
    assert scripted.calls == 3
    fourth = client.post("/api/chat", json={"conversation_id": conversation_id, "text": "one more thing"})
    assert scripted.calls == 3
    assert "specialist already has this chat" in fourth.json()["reply"]


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
