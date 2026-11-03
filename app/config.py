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
            whatsapp_verify_token=os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip(),
            whatsapp_token=os.getenv("WHATSAPP_TOKEN", "").strip(),
            whatsapp_phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip(),
            whatsapp_app_secret=os.getenv("WHATSAPP_APP_SECRET", "").strip(),
            sqlite_path=os.getenv("SQLITE_PATH", "data/travel.db").strip() or "data/travel.db",
            graph_version=os.getenv("WHATSAPP_GRAPH_VERSION", "v21.0").strip() or "v21.0",
        )
