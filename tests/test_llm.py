from types import SimpleNamespace

from app.extract import extract_facts
from app.knowledge import prompt_block
from app.llm import (
    QUALIFY_TOOL,
    AnthropicQualifier,
    QualificationDraft,
    RuleBasedQualifier,
    TurnContext,
    parse_qualification,
    to_api_messages,
)
from app.service import build_system


def test_sample_prompt_is_labelled_fictional():
    text = prompt_block()
    assert "SAMPLE DATA" in text
    assert "not a real agency" in text
    assert "[SAMPLE]" in text


def test_extract_full_sentence_and_ignore_a_false_name():
    facts = extract_facts(
        "We're 4 people going to Bali June 12-19 2026. Budget is 4000. My name is Jamie Lee."
    )
    assert facts["destination"] == "Bali, Indonesia"
    assert facts["travel_dates"] == "June 12-19 2026"
    assert facts["group_size"] == 4
    assert facts["budget_usd"] == 4000
    assert facts["contact_name"] == "Jamie Lee"

    looking = extract_facts("I'm looking at Bali")
    assert looking["contact_name"] is None
    assert looking["destination"] == "Bali, Indonesia"

    about = extract_facts("about 4 people")
    assert about["group_size"] == 4
    assert about["budget_usd"] is None


def test_dates_accept_a_rough_month_and_a_numeric_range():
    assert extract_facts("in June 2026")["travel_dates"] == "in June 2026"
    assert extract_facts("12-19 June")["travel_dates"].lower() == "12-19 june"
    assert extract_facts("next month")["travel_dates"] == "next month"


def test_rule_based_asks_one_question_then_summarises():
    qualifier = RuleBasedQualifier()
    first = qualifier.qualify(_turn("We're thinking Bali", known={}))
    assert first.destination == "Bali, Indonesia"
    assert "sample price" in first.reply
    assert first.reply.count("?") == 1

    known = {"destination": "Bali, Indonesia", "travel_dates": None, "group_size": None, "budget_usd": None, "contact_name": None}
    done = qualifier.qualify(
        _turn(
            "June 12-19 2026, 2 people, budget is 2500, my name is Sam Rivera",
            known=known,
        )
    )
    assert done.contact_name == "Sam Rivera"
    assert "Sam Rivera" in done.reply
    assert done.request_human is False


def test_tool_schema_is_strict():
    schema = QUALIFY_TOOL["input_schema"]
    assert QUALIFY_TOOL["strict"] is True
    assert schema["additionalProperties"] is False
    assert "reply" in schema["required"]
    assert "request_human" in schema["required"]
    assert "destination" in schema["required"]


def test_api_messages_start_with_a_user_turn():
    messages = to_api_messages(
        [
            {"role": "assistant", "content": "Hi"},
            {"role": "assistant", "content": "Still here"},
            {"role": "user", "content": "Bali"},
        ]
    )
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
    assert "Hi" in messages[1]["content"]
    assert "Still here" in messages[1]["content"]


def test_parse_tool_call_and_text_fallback():
    tool = SimpleNamespace(
        type="tool_use",
        name="record_lead_qualification",
        input={
            "reply": "June works.",
            "request_human": False,
            "destination": "Japan",
            "travel_dates": None,
            "group_size": "2",
            "budget_usd": 3100,
            "contact_name": "  ",
        },
    )
    draft = parse_qualification(SimpleNamespace(content=[tool]))
    assert draft.reply == "June works."
    assert draft.destination == "Japan"
    assert draft.group_size == 2
    assert draft.budget_usd == 3100
    assert draft.contact_name is None

    prose = parse_qualification(SimpleNamespace(content=[SimpleNamespace(type="text", text="Hello there")]))
    assert prose.reply == "Hello there"
    assert prose.destination is None
    assert prose.raw == {"unstructured": True}


def test_anthropic_qualifier_uses_auto_tool_choice(monkeypatch):
    captured = {}

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            block = SimpleNamespace(
                type="tool_use",
                name="record_lead_qualification",
                input={
                    "reply": "Where to?",
                    "request_human": False,
                    "destination": None,
                    "travel_dates": None,
                    "group_size": None,
                    "budget_usd": None,
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
    assert draft.reply == "Where to?"
    # Sonnet 5.5 returns 400 for tool_choice any/tool. Auto + strict is the supported path.
    assert captured["tool_choice"] == {"type": "auto"}
    assert captured["tools"][0]["strict"] is True
    assert captured["thinking"] == {"type": "between_tools"}
    assert captured["model"] == "claude-sonnet-5-5"
    assert captured["messages"][0]["role"] == "user"


def test_system_prompt_treats_the_ad_as_a_hint():
    system = build_system(
        {"destination": None},
        {
            "ad_id": "ad_bali_summer_01",
            "headline": "7 days in Bali from $1,299",
            "campaign": "meta_bali_summer_2026",
            "source": "facebook",
        },
        "Alex",
    )
    assert "SAMPLE DATA" in system
    assert "not a confirmed destination" in system
    assert "record_lead_qualification" in system
    assert "ad_bali_summer_01" in system


def _turn(text, known):
    base = {key: None for key in ("destination", "travel_dates", "group_size", "budget_usd", "contact_name")}
    base.update(known)
    return TurnContext(
        history=[{"role": "user", "content": text}],
        known=base,
        ad=None,
        profile_name=None,
        system="system",
    )
