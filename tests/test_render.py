from pathlib import Path


def test_render_blueprint_is_a_paid_docker_service():
    text = Path("render.yaml").read_text(encoding="utf-8")
    assert "runtime: docker" in text
    assert "plan: starter" in text
    assert "healthCheckPath: /api/health" in text
    for key in (
        "WHATSAPP_TOKEN",
        "PHONE_NUMBER_ID",
        "VERIFY_TOKEN",
        "APP_SECRET",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "GOOGLE_CALENDAR_ID",
        "GOOGLE_OAUTH_REFRESH_TOKEN",
        "STAFF_USERNAME",
        "STAFF_PASSWORD",
    ):
        assert key in text
    assert "twilio" not in text.lower()
    assert "n8n" not in text.lower()


def test_env_example_and_readme_document_the_manual_setup():
    example = Path(".env.example").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")
    for key in (
        "WHATSAPP_TOKEN",
        "PHONE_NUMBER_ID",
        "VERIFY_TOKEN",
        "APP_SECRET",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_CALENDAR_ID",
        "STAFF_USERNAME",
        "STAFF_PASSWORD",
    ):
        assert key in example
        assert key in readme
    assert "X-Hub-Signature-256" in readme
    assert "business verification" in readme.lower()
    assert "messages" in readme.lower()
    assert "Starter" in readme
