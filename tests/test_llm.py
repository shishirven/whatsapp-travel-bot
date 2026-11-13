from types import SimpleNamespace

from app.business import load_business
from app.extract import extract_facts
from app.llm import (
    QUALIFY_TOOL,
    AnthropicQualifier,
    QualificationDraft,
    RuleBasedQualifier,
    TurnContext,
    parse_qualification,
    to_api_messages,
)
from app.scoring import FACT_KEYS
from app.service import build_system


def test_prompt_lists_job_types_and_hides_prices():
    text = load_business().prompt_block()
    assert "Northside Electrical" in text
    assert "fault" in text
    assert "Newtown" in text
    assert "Do not mention prices" in text
    assert "$" not in text


def test_extract_a_job_and_ignore_a_false_name():
    facts = extract_facts(
        "The safety switch keeps tripping in Newtown. Not urgent. "
        "It trips when the oven is on. My name is Jamie Lee."
    )
    assert facts["job_type"] == "fault"
    assert facts["suburb"] == "Newtown"
    assert facts["urgency"] == "planned"
    assert "oven" in facts["description"]
    assert facts["contact_name"] == "Jamie Lee"

    looking = extract_facts("I'm looking at a switchboard in Marrickville")
    assert looking["contact_name"] is None
    assert looking["job_type"] == "switchboard"
    assert looking["suburb"] == "Marrickville"


def test_no_power_is_an_emergency_fault_and_an_unknown_suburb_is_kept():
    facts = extract_facts("No power in Bondi, sparks in the meter box right now")
    assert facts["job_type"] == "fault"
    assert facts["urgency"] == "emergency"
    assert facts["suburb"] == "Bondi"


def test_preferred_time_and_a_short_note_is_not_a_description():
    assert extract_facts("tomorrow morning")["preferred_time"] == "tomorrow morning"
    assert extract_facts("Newtown")["description"] is None


def test_rule_based_asks_one_question_then_summarises_without_a_price():
    qualifier = RuleBasedQualifier()
    first = qualifier.qualify(_turn("The safety switch keeps tripping"))
    assert first.job_type == "fault"
    assert first.reply.count("?") == 1
    assert "$" not in first.reply

    known = {key: None for key in FACT_KEYS}
    known.update({"job_type": "fault", "suburb": "Newtown"})
    done = qualifier.qualify(
        _turn(
            "Not urgent. The switch trips when the oven is on. My name is Sam Rivera",
            known=known,
        )
    )
    assert done.contact_name == "Sam Rivera"
    assert "Sam Rivera" in done.reply
    assert done.request_human is False
    assert "$" not in done.reply


def test_tool_schema_is_strict_and_has_no_price_field():
    schema = QUALIFY_TOOL["input_schema"]
    assert QUALIFY_TOOL["name"] == "record_job_intake"
    assert QUALIFY_TOOL["strict"] is True
    assert schema["additionalProperties"] is False
    assert "reply" in schema["required"]
    assert "request_human" in schema["required"]
    assert "job_type" in schema["required"]
    assert "budget_usd" not in schema["properties"]
    assert "destination" not in schema["properties"]


def test_api_messages_start_with_a_user_turn():
    messages = to_api_messages(
        [
            {"role": "assistant", "content": "Hi"},
            {"role": "assistant", "content": "Still here"},
            {"role": "user", "content": "Newtown"},
        ]
    )
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
    assert "Hi" in messages[1]["content"]
    assert "Still here" in messages[1]["content"]


def test_parse_tool_call_and_text_fallback():
    tool = SimpleNamespace(
        type="tool_use",
        name="record_job_intake",
        input={
            "reply": "Newtown is covered.",
            "request_human": False,
            "job_type": "switchboard",
            "suburb": "Newtown",
            "urgency": "EMERGENCY",
            "description": "  ",
            "preferred_time": None,
            "contact_name": "  ",
        },
    )
    draft = parse_qualification(SimpleNamespace(content=[tool]))
    assert draft.reply == "Newtown is covered."
    assert draft.job_type == "switchboard"
    assert draft.suburb == "Newtown"
    assert draft.urgency == "emergency"
    assert draft.description is None
    assert draft.contact_name is None

    prose = parse_qualification(
        SimpleNamespace(content=[SimpleNamespace(type="text", text="Hello there")])
    )
    assert prose.reply == "Hello there"
    assert prose.job_type is None
    assert prose.raw == {"unstructured": True}


def test_anthropic_qualifier_uses_auto_tool_choice(monkeypatch):
    captured = {}

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            block = SimpleNamespace(
                type="tool_use",
                name="record_job_intake",
                input={
                    "reply": "Which suburb?",
                    "request_human": False,
                    "job_type": None,
                    "suburb": None,
                    "urgency": None,
                    "description": None,
                    "preferred_time": None,
                    "contact_name": None,
                },
            )
            return SimpleNamespace(content=[block])

    qualifier = AnthropicQualifier(
        "test-key",
        "claude-sonnet-5-5",
        client=SimpleNamespace(messages=FakeMessages()),
    )
    draft = qualifier.qualify(_turn("Hi", known={}))
    assert isinstance(draft, QualificationDraft)
    assert draft.reply == "Which suburb?"
    # Sonnet 5.5 returns 400 for tool_choice any/tool. Auto + strict is the supported path.
    assert captured["tool_choice"] == {"type": "auto"}
    assert captured["tools"][0]["strict"] is True
    assert captured["thinking"] == {"type": "between_tools"}
    assert captured["model"] == "claude-sonnet-5-5"
    assert captured["messages"][0]["role"] == "user"


def test_system_prompt_treats_the_ad_as_a_hint_and_hides_prices():
    system = build_system(
        {"job_type": None},
        {
            "ad_id": "ad_fault_01",
            "headline": "Emergency electrician — Inner West",
            "campaign": "meta_emergency_electrician",
            "source": "facebook",
        },
        "Alex",
    )
    assert "Northside Electrical" in system
    assert "Do not mention prices" in system
    assert "not a confirmed job" in system
    assert "record_job_intake" in system
    assert "ad_fault_01" in system
    assert "call 000" in system
    assert "$" not in system


def _turn(text, known=None):
    base = {key: None for key in FACT_KEYS}
    base.update(known or {})
    return TurnContext(
        history=[{"role": "user", "content": text}],
        known=base,
        ad=None,
        profile_name=None,
        system="system",
    )
