"""Customers, jobs, import, and the inbound routing rules.

No test in this file calls Anthropic, Meta, OpenAI, or Google. Classifiers are
fakes or the offline phrase list.
"""

import json
import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.customers_io import import_csv_text
from app.db import Database
from app.import_clients import main as import_main
from app.llm import QualificationDraft
from app.main import create_app
from app.phones import normalise_au_phone
from app.routing import (
    ADDRESS_SEARCH_QUESTION,
    LLMIntentClassifier,
    PRIOR_CLIENT_QUESTION,
    KeywordIntentClassifier,
    classify_source,
    evaluate_soft_match,
)
from app.business import load_business
from tests.conftest import make_settings
from tests.fakes import MemoryGateway


def test_australian_numbers_become_whatsapp_digits():
    assert normalise_au_phone("0412 345 678") == "61412345678"
    assert normalise_au_phone("+61 412 345 678") == "61412345678"
    assert normalise_au_phone("+61 (0) 412 345 678") == "61412345678"
    assert normalise_au_phone("(02) 9555-1234") == "61295551234"
    assert normalise_au_phone("61412345678") == "61412345678"
    assert normalise_au_phone("412345678") == "61412345678"
    assert normalise_au_phone("+1 555 123 0000") == "15551230000"
    assert normalise_au_phone("nope") is None
    assert normalise_au_phone("") is None


def test_source_tags_beat_a_referral_and_repeat_is_the_fallback():
    business = load_business()

    class Inbound:
        def __init__(self, ad_id=None, headline=None):
            self.ad_id = ad_id
            self.ad_headline = headline

    source, text = classify_source(
        "Hi (WEB) the switchboard is buzzing",
        inbound=Inbound(ad_id="ad_fault_01", headline="Emergency"),
        returning=True,
        business=business,
    )
    assert source == "website"
    assert "(WEB)" not in text
    assert "switchboard" in text

    source, text = classify_source(
        "Hello [qr]",
        inbound=Inbound(),
        returning=False,
        business=business,
    )
    assert source == "qr"
    assert text == "Hello"

    source, _text = classify_source(
        "Hi",
        inbound=Inbound(ad_id="ad_fault_01"),
        returning=True,
        business=business,
    )
    assert source == "ad"

    source, _text = classify_source(
        "Hi", inbound=Inbound(), returning=True, business=business
    )
    assert source == "repeat"

    source, _text = classify_source(
        "Hi", inbound=Inbound(), returning=False, business=business
    )
    assert source == "organic"


def test_soft_match_flags_one_client_and_asks_only_when_it_is_unclear():
    candidates = [
        {"id": 4, "name": "Pat Existing", "addresses": ["88 Secret Lane, Newtown"]},
        {"id": 5, "name": "Sam Lee", "addresses": ["Marrickville"]},
    ]
    flagged = evaluate_soft_match(
        name="Pat Existing",
        suburb="Newtown",
        address=None,
        text="My name is Pat Existing. Newtown.",
        asked=False,
        answer=None,
        address_prompted=False,
        candidates=candidates,
    )
    assert flagged["possible_customer_id"] == 4
    assert flagged["question"] is None

    unclear = evaluate_soft_match(
        name="Pat Existing",
        suburb=None,
        address=None,
        text="My name is Pat Existing",
        asked=False,
        answer=None,
        address_prompted=False,
        candidates=candidates,
    )
    assert unclear["question"] == PRIOR_CLIENT_QUESTION
    assert unclear["possible_customer_id"] is None

    again = evaluate_soft_match(
        name="Pat Existing",
        suburb=None,
        address=None,
        text="Not urgent",
        asked=True,
        answer=None,
        address_prompted=False,
        candidates=candidates,
    )
    assert again["question"] is None

    yes = evaluate_soft_match(
        name="Pat Existing",
        suburb=None,
        address=None,
        text="Yes",
        asked=True,
        answer=None,
        address_prompted=False,
        candidates=candidates,
    )
    assert yes["answer"] == "yes"
    assert yes["question"] == ADDRESS_SEARCH_QUESTION

    declined = evaluate_soft_match(
        name="Pat Existing",
        suburb="Newtown",
        address=None,
        text="No",
        asked=True,
        answer=None,
        address_prompted=False,
        candidates=candidates,
    )
    assert declined["answer"] == "no"
    assert declined["question"] is None
    assert declined["possible_customer_id"] == 4


def test_llm_intent_reads_yes_or_no_from_a_fake_client():
    captured = {}

    class FakeMessages:
        def __init__(self, answer):
            self.answer = answer

        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=self.answer)]
            )

    yes = LLMIntentClassifier(
        "test-key",
        "claude-sonnet-5-5",
        client=SimpleNamespace(messages=FakeMessages("YES")),
    )
    assert yes.classify("can we do the other house too", job_summary="fault, Newtown") is True
    assert captured["model"] == "claude-sonnet-5-5"
    assert "YES" in captured["system"]
    assert "price" not in captured["system"].lower()

    no = LLMIntentClassifier(
        "test-key",
        "claude-sonnet-5-5",
        client=SimpleNamespace(messages=FakeMessages("NO")),
    )
    assert no.classify("the oven still trips it", job_summary="fault") is False

    offline = KeywordIntentClassifier()
    assert offline.classify("Also, a switchboard in Newtown") is False
    assert offline.classify("This is a different job") is True


def test_legacy_sqlite_becomes_a_customer_and_one_job(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wa_id TEXT NOT NULL UNIQUE,
                profile_name TEXT,
                source TEXT NOT NULL,
                ad_id TEXT,
                campaign TEXT,
                ad_headline TEXT,
                ad_source TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                direction TEXT NOT NULL,
                body TEXT NOT NULL,
                provider_message_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL UNIQUE,
                job_type TEXT,
                suburb TEXT,
                urgency TEXT,
                description TEXT,
                preferred_time TEXT,
                contact_name TEXT,
                score TEXT NOT NULL,
                score_reason TEXT NOT NULL,
                handoff INTEGER NOT NULL DEFAULT 0,
                handoff_reason TEXT,
                photos_json TEXT NOT NULL DEFAULT '[]',
                voice_json TEXT NOT NULL DEFAULT '[]',
                quote_json TEXT NOT NULL DEFAULT '{}',
                booking_json TEXT NOT NULL DEFAULT '{}',
                extracted_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO conversations (
                id, wa_id, profile_name, source, ad_id, campaign, ad_headline,
                ad_source, status, created_at, updated_at
            ) VALUES (3, '15551230000', 'Jamie', 'whatsapp', 'ad_fault_01',
                      'meta_emergency_electrician', 'Emergency electrician — Inner West',
                      'facebook', 'handed_off', '2024-01-01 00:00:00 UTC',
                      '2024-01-02 00:00:00 UTC')
            """
        )
        conn.execute(
            """
            INSERT INTO leads (
                id, conversation_id, job_type, suburb, urgency, description,
                contact_name, score, score_reason, handoff, handoff_reason,
                booking_json, created_at, updated_at
            ) VALUES (7, 3, 'fault', 'Newtown', 'planned', 'Oven trips the switch',
                      'Jamie Lee', 'hot', 'Complete', 1, 'hot_lead',
                      '{"status": "booked", "label": "Mon 9:00"}',
                      '2024-01-01 00:00:00 UTC', '2024-01-02 00:00:00 UTC')
            """
        )
        conn.execute(
            """
            INSERT INTO messages (conversation_id, direction, body, provider_message_id, created_at)
            VALUES (3, 'in', 'hello from the old thread', 'wamid.old', '2024-01-01 00:00:00 UTC')
            """
        )

    db = Database(str(path))
    customer = db.get_customer_by_wa("15551230000")
    assert customer["name"] == "Jamie Lee"
    assert customer["trusted"] == 1
    assert "Newtown" in customer["addresses_json"]
    job = db.get_job(7)
    assert job["suburb"] == "Newtown"
    assert job["job_type"] == "fault"
    assert job["job_source"] == "ad"
    assert job["job_status"] == "booked"
    assert job["campaign"] == "meta_emergency_electrician"
    assert db.get_conversation(3)["active_job_id"] == 7
    messages = db.list_messages(job_id=7)
    assert messages[0]["body"] == "hello from the old thread"
    assert messages[0]["job_id"] == 7

    Database(str(path))
    assert db.get_job(7)["contact_name"] == "Jamie Lee"
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'leads'"
        ).fetchone() is None
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1


def test_csv_import_normalises_phones_and_the_cli_can_dry_run(tmp_path):
    path = tmp_path / "tradie.db"
    csv_path = tmp_path / "clients.csv"
    csv_path.write_text(
        "name,phones,address,notes\n"
        "Sam Lee,0412 345 678; (02) 9555-1234,\"12 Smith St, Newtown\",Prefers mornings\n"
        "Bad Row,not-a-phone,Bondi,skip me\n",
        encoding="utf-8",
    )
    assert import_main([str(csv_path), "--db", str(path), "--dry-run"]) == 1
    db = Database(str(path))
    assert db.list_customers() == []

    assert import_main([str(csv_path), "--db", str(path)]) == 1
    customer = db.get_customer_by_wa("61412345678")
    assert customer["name"] == "Sam Lee"
    assert customer["trusted"] == 1
    assert "12 Smith St, Newtown" in customer["addresses_json"]
    assert "Prefers mornings" in customer["notes"]
    assert db.get_customer_by_wa("61295551234")["id"] == customer["id"]
    assert db.list_jobs_for_customer(customer["id"]) == []

    again = import_csv_text(
        db,
        "name,phone,address,notes\nSam Lee,0412345678,\"12 Smith St, Newtown\",Gate is on the left\n",
    )
    assert again.created == 0
    assert again.updated == 1
    assert "Gate is on the left" in db.get_customer(customer["id"])["notes"]


def test_imported_client_is_greeted_by_name_and_not_reasked(tmp_path, notifier):
    settings = make_settings(tmp_path)
    client = TestClient(create_app(settings=settings, notifier=notifier))
    db = client.app.state.db
    db.import_client(
        name="Sam Lee",
        phones=["0412 345 678"],
        address="12 Smith St, Newtown",
        notes="Prefers mornings",
    )
    sent = _webhook(
        client,
        "Need a ceiling fan",
        wa_id="61412345678",
        message_id="wamid.sam",
        name="Sam",
    )
    assert sent.json()["processed"] == 1
    detail = client.get("/staff/1")
    assert detail.status_code == 200
    assert "Sam Lee" in detail.text
    assert "repeat" in detail.text
    assert "12 Smith St, Newtown" in detail.text
    assert "Which suburb is the job in?" not in detail.text
    assert "What name should I put on the job?" not in detail.text
    job = db.get_job(1)
    assert job["job_source"] == "repeat"
    assert job["job_type"] == "ceiling_fan"
    assert job["suburb"] == "Newtown"
    assert job["contact_name"] == "Sam Lee"
    assert "$" not in _outbound(db, job["id"])


def test_new_number_is_flagged_without_being_told_the_history(tmp_path, notifier):
    class Capture:
        def __init__(self):
            self.systems = []

        def qualify(self, turn):
            self.systems.append(turn.system)
            return QualificationDraft(
                reply="What needs doing?",
                contact_name="Pat Existing",
                suburb="Newtown",
                job_type="fault",
            )

    capture = Capture()
    settings = make_settings(tmp_path)
    client = TestClient(
        create_app(settings=settings, notifier=notifier, qualifier=capture)
    )
    db = client.app.state.db
    db.import_client(
        name="Pat Existing",
        phones=["61400000001"],
        address="88 Secret Lane, Newtown",
        notes="gate code 9911",
    )
    customer = db.get_customer_by_wa("61400000001")
    conversation = db.create_conversation(
        wa_id="61400000001",
        profile_name="Pat",
        source="whatsapp",
        customer_id=customer["id"],
        job_source="repeat",
        prefill={"contact_name": "Pat Existing", "suburb": "Newtown"},
        address="88 Secret Lane, Newtown",
    )
    db.save_job(
        conversation["active_job_id"],
        {
            "job_type": "fault",
            "suburb": "Newtown",
            "urgency": "planned",
            "description": "gate code 9911 was used last time",
            "preferred_time": None,
            "contact_name": "Pat Existing",
            "score": "hot",
            "score_reason": "done",
            "handoff": 1,
            "handoff_reason": "hot_lead",
            "quote_json": json.dumps(
                {"min_aud": 1800, "max_aud": 2200, "status": "sent", "label": "Fault"}
            ),
            "booking_json": "{}",
        },
    )

    _webhook(
        client,
        "My name is Pat Existing. The power point is in Newtown.",
        wa_id="61400000099",
        message_id="wamid.new",
        name="Pat",
    )
    assert capture.systems
    secret_bits = ("88 Secret", "9911", "1800", "2200")
    for system in capture.systems:
        for bit in secret_bits:
            assert bit not in system
        assert "not a confirmed existing client" in system
    new_job = db.get_lead_for_conversation(
        db.get_conversation_by_wa("61400000099")["id"]
    )
    outbound = _outbound(db, new_job["id"])
    for bit in secret_bits:
        assert bit not in outbound
    assert "welcome back" not in outbound.lower()
    assert new_job["possible_customer_id"] == customer["id"]
    page = client.get(f"/staff/{new_job['id']}")
    assert "Possible existing client" in page.text
    assert "Pat Existing" in page.text

    linked = client.post(
        f"/staff/{new_job['id']}/link",
        data={"customer_id": str(customer["id"])},
        follow_redirects=False,
    )
    assert linked.status_code == 303
    assert linked.headers["location"] == f"/staff/customers/{customer['id']}"
    owned = db.get_customer_by_wa("61400000099")
    assert owned["id"] == customer["id"]
    desk = client.get(f"/staff/customers/{customer['id']}")
    assert "61400000099" in desk.text
    assert "61400000001" in desk.text


def test_unclear_match_is_asked_once_then_an_address_is_requested(tmp_path, notifier):
    settings = make_settings(tmp_path)
    client = TestClient(create_app(settings=settings, notifier=notifier))
    db = client.app.state.db
    db.import_client(
        name="Alex Rivera",
        phones=["61411111111"],
        address="Marrickville",
        notes=None,
    )
    _webhook(client, "My name is Alex Rivera", wa_id="61422222222", message_id="wamid.a1")
    first = _outbound_all(db)
    assert PRIOR_CLIENT_QUESTION in first
    _webhook(client, "Not urgent", wa_id="61422222222", message_id="wamid.a2")
    second = _outbound(db, db.get_conversation_by_wa("61422222222")["active_job_id"])
    assert second.count(PRIOR_CLIENT_QUESTION) == 1
    _webhook(client, "Yes", wa_id="61422222222", message_id="wamid.a3")
    third = _outbound(db, db.get_conversation_by_wa("61422222222")["active_job_id"])
    assert ADDRESS_SEARCH_QUESTION in third
    assert third.count(PRIOR_CLIENT_QUESTION) == 1


def test_prefilled_tag_sets_the_job_source(tmp_path, notifier):
    settings = make_settings(tmp_path)
    client = TestClient(create_app(settings=settings, notifier=notifier))
    _webhook(
        client,
        "Hi (web) need a power point in Newtown",
        wa_id="61433333333",
        message_id="wamid.web",
        name="Casey",
    )
    page = client.get("/staff/1")
    assert "website" in page.text
    assert "Power point" in page.text
    job = client.app.state.db.get_job(1)
    assert job["job_source"] == "website"
    assert job["job_type"] == "power_point"


def test_completed_job_starts_a_new_one_and_an_open_job_can_be_split(tmp_path, notifier):
    settings = make_settings(tmp_path)
    gateway = MemoryGateway()
    client = TestClient(create_app(settings=settings, notifier=notifier, gateway=gateway))
    started = client.post("/api/conversations", json={"profile_name": "Jamie"}).json()
    conversation_id = started["conversation_id"]
    first = client.post(
        "/api/chat",
        json={
            "conversation_id": conversation_id,
            "text": (
                "The safety switch keeps tripping in Newtown. Not urgent. "
                "It trips when the oven is on. My name is Jamie Lee."
            ),
        },
    ).json()
    old_id = first["lead"]["id"]
    assert first["lead"]["handoff"] is True
    booked = client.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "text": "1"},
    ).json()
    assert booked["lead"]["booking"]["status"] == "booked"
    when = booked["lead"]["booking"]["label"]
    status = client.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "text": "what time am I booked?"},
    ).json()
    assert "You're booked" in status["reply"]
    assert when in status["reply"]
    assert "$" not in status["reply"]
    assert status["lead"]["job_type"] == "fault"

    client.post(
        f"/staff/{old_id}/status",
        data={"status": "completed"},
        follow_redirects=False,
    )
    again = client.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "text": "Need a smoke alarm"},
    ).json()
    assert again["lead"]["id"] != old_id
    assert again["lead"]["job_type"] == "smoke_alarm"
    assert again["lead"]["source"] == "repeat"
    assert again["lead"]["suburb"] == "Newtown"
    assert again["lead"]["contact_name"] == "Jamie Lee"
    assert "Jamie Lee" in again["reply"]
    assert "Newtown" in again["reply"]
    assert "What name should I put on the job?" not in again["reply"]
    assert "oven" not in (again["lead"]["description"] or "")
    assert "$" not in again["reply"]
    old = client.get(f"/staff/{old_id}")
    assert "completed" in old.text
    assert "oven" in old.text.lower() or "Oven" in old.text or "oven" in old.text

    extra = client.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "text": "purple-pelican is the side gate"},
    ).json()
    # Still the new job, and not a different-job phrase, so intake continues.
    assert extra["lead"]["id"] == again["lead"]["id"]
    db = client.app.state.db
    message_id = next(
        row["id"]
        for row in db.list_messages(job_id=extra["lead"]["id"])
        if "purple-pelican" in row["body"]
    )
    split = client.post(
        f"/staff/{extra['lead']['id']}/split",
        data={"message_id": str(message_id)},
    )
    assert split.status_code == 200
    assert "purple-pelican" in split.text
    left = client.get(f"/staff/{extra['lead']['id']}")
    assert "purple-pelican" not in left.text
    new_job_id = db.get_conversation(conversation_id)["active_job_id"]
    moved = client.post(
        f"/staff/messages/{message_id}/reassign",
        data={"job_id": str(extra["lead"]["id"])},
    )
    assert moved.status_code == 200
    assert "purple-pelican" in client.get(f"/staff/{extra['lead']['id']}").text
    assert "purple-pelican" not in client.get(f"/staff/{new_job_id}").text
    assert gateway.sent


def test_open_job_follow_up_does_not_restart_and_a_new_job_does(tmp_path, notifier):
    class Scripted:
        def __init__(self):
            self.calls = 0

        def qualify(self, turn):
            self.calls += 1
            latest = turn.history[-1]["content"]
            if "ceiling fan" in latest:
                return QualificationDraft(
                    reply="A ceiling fan. Which suburb?",
                    job_type="ceiling_fan",
                )
            return QualificationDraft(reply="Fault finding. Which suburb?", job_type="fault")

    class Intent:
        def classify(self, text, job_summary=""):
            return "different job" in text.lower()

    scripted = Scripted()
    settings = make_settings(tmp_path)
    client = TestClient(
        create_app(settings=settings, notifier=notifier, qualifier=scripted, intent=Intent())
    )
    conversation_id = client.post("/api/conversations", json={}).json()["conversation_id"]
    first = client.post(
        "/api/chat",
        json={"conversation_id": conversation_id, "text": "the safety switch keeps tripping"},
    ).json()
    assert first["lead"]["job_type"] == "fault"
    assert scripted.calls == 1

    second = client.post(
        "/api/chat",
        json={
            "conversation_id": conversation_id,
            "text": "this is a different job, a ceiling fan",
        },
    ).json()
    assert scripted.calls == 2
    assert "separate job" in second["reply"].lower()
    assert second["lead"]["id"] != first["lead"]["id"]
    assert second["lead"]["job_type"] == "ceiling_fan"
    assert second["lead"]["source"] == "repeat"
    original = client.app.state.db.get_job(first["lead"]["id"])
    assert original["job_type"] == "fault"
    assert original["job_status"] == "open"

    photo = _image_webhook(
        client,
        wa_id=second["wa_id"],
        message_id="wamid.photo",
        media_id="pic-1",
    )
    assert photo.json()["processed"] == 1
    # The ceiling-fan job is still open, so the photo stays on it and intake does not restart.
    assert scripted.calls == 2
    page = client.get(f"/staff/{second['lead']['id']}")
    assert "pic-1" in page.text


def test_ad_referral_opens_with_the_headline_and_campaign(tmp_path, notifier):
    settings = make_settings(tmp_path)
    client = TestClient(create_app(settings=settings, notifier=notifier))
    payload = _payload(
        "Hi",
        wa_id="15557650001",
        message_id="wamid.ad",
        name="Alex",
        referral={
            "source_url": "https://fb.me/sparky",
            "source_id": "ad_fault_01",
            "source_type": "ad",
            "headline": "Emergency electrician — Inner West",
        },
    )
    assert client.post("/webhook", json=payload).status_code == 200
    db = client.app.state.db
    job = db.get_job(1)
    assert job["job_source"] == "ad"
    assert job["campaign"] == "meta_emergency_electrician"
    outbound = _outbound(db, job["id"])
    assert "Emergency electrician — Inner West" in outbound
    assert "meta_emergency_electrician" in outbound
    assert "$" not in outbound


def test_staff_can_upload_a_client_csv(tmp_path, notifier):
    client = TestClient(create_app(settings=make_settings(tmp_path), notifier=notifier))
    page = client.post(
        "/staff/customers/import",
        files={
            "file": (
                "clients.csv",
                "name,phone,address,notes\nSam Lee,0412 345 678,Newtown,mornings\n",
                "text/csv",
            )
        },
    )
    assert page.status_code == 200
    assert "Created 1" in page.text
    assert "Sam Lee" in page.text
    assert "61412345678" in page.text
    assert client.app.state.db.get_customer_by_wa("61412345678")["notes"] == "mornings"


def test_a_handed_off_job_keeps_a_photo_without_restarting_intake(tmp_path, notifier):
    class Once:
        def __init__(self):
            self.calls = 0

        def qualify(self, turn):
            self.calls += 1
            return QualificationDraft(reply="I'll bring someone in.", request_human=True)

    once = Once()
    client = TestClient(create_app(settings=make_settings(tmp_path), notifier=notifier, qualifier=once))
    started = client.post("/api/conversations", json={}).json()
    client.post(
        "/api/chat",
        json={"conversation_id": started["conversation_id"], "text": "Can I talk to a person?"},
    )
    assert once.calls == 1
    image = _image_webhook(
        client,
        wa_id=started["wa_id"],
        message_id="wamid.after",
        media_id="meter-2",
    )
    assert image.json()["processed"] == 1
    assert once.calls == 1
    page = client.get("/staff/1")
    assert "meter-2" in page.text
    assert "Photo" in page.text


def test_staff_login_gates_customer_pages(tmp_path, notifier):
    settings = make_settings(
        tmp_path,
        staff_username="desk",
        staff_password="secret",
        staff_session_secret="session-secret",
    )
    client = TestClient(create_app(settings=settings, notifier=notifier))
    blocked = client.get("/staff/customers", follow_redirects=False)
    assert blocked.status_code == 303
    assert blocked.headers["location"] == "/staff/login"
    denied = client.post(
        "/staff/customers/import",
        files={"file": ("clients.csv", b"name,phone\nSam,0412345678\n", "text/csv")},
    )
    assert denied.status_code == 401
    assert client.post("/staff/1/link", data={"customer_id": "1"}).status_code == 401
    assert client.post("/staff/1/split", data={"message_id": "1"}).status_code == 401
    assert client.post("/staff/1/status", data={"status": "completed"}).status_code == 401
    assert client.post(
        "/staff/messages/1/reassign", data={"job_id": "2"}
    ).status_code == 401

    client.post("/staff/login", data={"username": "desk", "password": "secret"})
    page = client.get("/staff/customers")
    assert page.status_code == 200
    assert "No customers yet" in page.text or "Customers" in page.text


def _webhook(client, text, *, wa_id, message_id, name="Jamie"):
    return client.post(
        "/webhook",
        json=_payload(text, wa_id=wa_id, message_id=message_id, name=name),
    )


def _image_webhook(client, *, wa_id, message_id, media_id):
    payload = _payload(None, wa_id=wa_id, message_id=message_id, name="Jamie")
    payload["entry"][0]["changes"][0]["value"]["messages"] = [
        {
            "from": wa_id,
            "id": message_id,
            "type": "image",
            "image": {"id": media_id},
        }
    ]
    return client.post("/webhook", json=payload)


def _payload(body, *, wa_id, message_id, name, referral=None):
    message = {
        "from": wa_id,
        "id": message_id,
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
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "contacts": [{"profile": {"name": name}, "wa_id": wa_id}],
                            "messages": [message],
                        },
                    }
                ]
            }
        ]
    }


def _outbound(db, job_id) -> str:
    return "\n".join(
        row["body"]
        for row in db.list_messages(job_id=job_id)
        if row["direction"] == "out"
    )


def _outbound_all(db) -> str:
    chunks = []
    for job in db.list_jobs():
        chunks.append(_outbound(db, job["id"]))
    return "\n".join(chunks)
