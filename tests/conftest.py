import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.notify import ListNotifier


def make_settings(tmp_path, **overrides) -> Settings:
    data = dict(
        anthropic_api_key="",
        anthropic_model="claude-sonnet-5-5",
        llm_provider="mock",
        messaging_provider="simulated",
        whatsapp_verify_token="test-token",
        whatsapp_token="",
        whatsapp_phone_number_id="",
        whatsapp_app_secret="",
        sqlite_path=str(tmp_path / "test.db"),
        graph_version="v21.0",
    )
    data.update(overrides)
    return Settings(**data)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def notifier():
    return ListNotifier()


@pytest.fixture
def client(settings, notifier):
    application = create_app(settings=settings, notifier=notifier)
    return TestClient(application)
