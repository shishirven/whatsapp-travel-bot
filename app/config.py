"""Environment settings. Secrets stay in the environment, never in code."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

_LLM_PROVIDERS = {"mock", "anthropic"}
_MESSAGING_PROVIDERS = {"simulated", "whatsapp"}


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str
    anthropic_model: str
    llm_provider: str
    messaging_provider: str
    whatsapp_verify_token: str
    whatsapp_token: str
    whatsapp_phone_number_id: str
    whatsapp_app_secret: str
    sqlite_path: str
    graph_version: str = "v21.0"
    openai_api_key: str = ""
    whisper_model: str = "whisper-1"
    google_service_account_json: str = ""
    google_credentials_file: str = ""
    google_calendar_id: str = ""
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_refresh_token: str = ""
    staff_username: str = ""
    staff_password: str = ""
    staff_session_secret: str = ""
    business_config_path: str = ""

    def __post_init__(self) -> None:
        if self.llm_provider not in _LLM_PROVIDERS:
            raise ValueError(
                f"LLM_PROVIDER must be one of {sorted(_LLM_PROVIDERS)}, got {self.llm_provider!r}"
            )
        if self.messaging_provider not in _MESSAGING_PROVIDERS:
            raise ValueError(
                "MESSAGING_PROVIDER must be one of "
                f"{sorted(_MESSAGING_PROVIDERS)}, got {self.messaging_provider!r}"
            )

    @property
    def staff_auth_enabled(self) -> bool:
        return bool(self.staff_username and self.staff_password)

    @classmethod
    def from_env(cls) -> Settings:
        # Do not override variables already set in the process environment.
        load_dotenv(override=False)
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        provider = os.getenv("LLM_PROVIDER", "").strip().lower()
        if not provider:
            # A fresh clone with no key still answers, via the offline qualifier.
            provider = "anthropic" if key else "mock"
        return cls(
            anthropic_api_key=key,
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5").strip()
            or "claude-sonnet-5-5",
            llm_provider=provider,
            messaging_provider=os.getenv("MESSAGING_PROVIDER", "simulated").strip().lower()
            or "simulated",
            whatsapp_verify_token=_env("VERIFY_TOKEN", "WHATSAPP_VERIFY_TOKEN"),
            whatsapp_token=_env("WHATSAPP_TOKEN"),
            whatsapp_phone_number_id=_env("PHONE_NUMBER_ID", "WHATSAPP_PHONE_NUMBER_ID"),
            whatsapp_app_secret=_env("APP_SECRET", "WHATSAPP_APP_SECRET"),
            sqlite_path=os.getenv("SQLITE_PATH", "data/tradie.db").strip() or "data/tradie.db",
            graph_version=os.getenv("WHATSAPP_GRAPH_VERSION", "v21.0").strip() or "v21.0",
            openai_api_key=_env("OPENAI_API_KEY"),
            whisper_model=os.getenv("WHISPER_MODEL", "whisper-1").strip() or "whisper-1",
            google_service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip(),
            google_credentials_file=_env(
                "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CREDENTIALS_FILE"
            ),
            google_calendar_id=_env("GOOGLE_CALENDAR_ID"),
            google_oauth_client_id=_env("GOOGLE_OAUTH_CLIENT_ID"),
            google_oauth_client_secret=_env("GOOGLE_OAUTH_CLIENT_SECRET"),
            google_oauth_refresh_token=_env("GOOGLE_OAUTH_REFRESH_TOKEN"),
            staff_username=_env("STAFF_USERNAME"),
            staff_password=_env("STAFF_PASSWORD"),
            staff_session_secret=_env("STAFF_SESSION_SECRET"),
            business_config_path=_env("BUSINESS_CONFIG"),
        )


def _env(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return ""
