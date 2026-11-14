"""
Configuration — reads from environment variables / .env file.

All settings have safe defaults so the service starts with just ANTHROPIC_API_KEY.
Previously used GEMINI_API_KEY; now replaced with Anthropic Claude API.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Service identity ──────────────────────────────────────────────────────
    service_name:    str     = "job-parser-service"
    service_version: str     = "1.0.0"
    environment:     Literal["development", "staging", "production"] = "development"
    debug:           bool    = False

    # ── Auth ──────────────────────────────────────────────────────────────────
    # Clients must send   X-API-Key: <value>
    # Leave empty to disable auth (not recommended in production)
    api_key: str = ""

    # ── Anthropic Claude ──────────────────────────────────────────────────────
    anthropic_api_key:    str   = ""
    anthropic_model:      str   = "claude-sonnet-4-6"
    anthropic_max_tokens: int   = 2048
    anthropic_timeout_secs: int = 30           # per-request timeout
    anthropic_max_retries:  int = 3            # tenacity retry attempts

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Kept as a plain comma-separated string: pydantic-settings JSON-decodes list
    # fields read from the environment, so CORS_ORIGINS="http://a,http://b" (the
    # format the main app's .env uses) would fail to load. Use cors_origins_list.
    cors_origins: str = "*"

    @property
    def cors_origins_list(self) -> list[str]:
        """CORS_ORIGINS split on commas, blanks dropped (["*"] when empty)."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()] or ["*"]

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
