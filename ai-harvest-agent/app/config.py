"""Application configuration via pydantic-settings."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Anchored to the ai-harvest-agent project root (parent of this file's
        # directory) so it resolves correctly regardless of the process's CWD —
        # ".env" alone breaks when launched from the repo root instead of from
        # inside ai-harvest-agent/, and "ai-harvest-agent/.env" breaks when
        # launched (as documented) from inside ai-harvest-agent/ itself.
        env_file=str(Path(__file__).resolve().parent.parent / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────────────────────────────
    app_name: str = "AI Harvest Agent"
    app_env: Literal["development", "staging", "production"] = "development"
    app_debug: bool = False
    app_secret_key: str = "change-me-in-production"
    api_key: str = "dev-api-key"
    api_v1_prefix: str = "/api/v1"

    # Console log verbosity (DEBUG/INFO/WARNING/ERROR). The rotating debug
    # file at data/logs/app.log always captures DEBUG regardless of this —
    # this only controls what prints to the terminal.
    log_level: str = "INFO"

    # ── Harvest results read source ────────────────────────────────────────────
    # "auto"     — DB first, fall back to the JSON/file store if the DB has no
    #              rows for that query or is unreachable (default).
    # "database" — DB only; never fall back, even when empty (surfaces DB bugs
    #              immediately instead of silently masking them with old JSON).
    # "json"     — always read the JSON/file store; skip the DB read entirely.
    # Applies to GET /harvest-status, /run-history, /{linkedin,naukri,dice}-results.
    # Writes are unaffected — every run is still mirrored to the DB regardless.
    data_source: Literal["auto", "database", "json"] = "database"

    # ── Database ─────────────────────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./data/harvest.db"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    # ── Redis ────────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # ── Anthropic ────────────────────────────────────────────────────────────────
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"
    anthropic_max_tokens: int = 8096
    anthropic_temperature: float = 0.0

    # ── HTML extraction LLM provider ──────────────────────────────────────────────
    # Selects which model LLMService.extract_json() uses (the LinkedIn HTML
    # extraction fallback and any other extract_json() caller).
    #   ""  or "claude" / "claude-*"     -> Anthropic Claude (anthropic_model, or the
    #                                       specific claude-* id given here)
    #   "ollama"                         -> local LLM at local_llm_url, model = local_llm_model
    #   "openrouter"                     -> OpenRouter, model = openrouter_model
    #   "openrouter/<provider>/<model>"  -> OpenRouter, using that model id directly
    #   any other value                  -> local LLM at local_llm_url, used as the model name
    extraction_llm_model: str = "claude"
    local_llm_url:        str = "http://localhost:11434"
    local_llm_model:      str = "llama3.1:8b"

    # ── OpenRouter (fallback LLM for HTML extraction) ──────────────────────────────
    openrouter_api_key: str = ""
    openrouter_model:   str = "google/gemma-4-26b-a4b-it:free"

    # ── Google Gemini ────────────────────────────────────────────────────────────
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    gemini_max_output_tokens: int = 2048
    gemini_temperature: float = 0.0

    # ── Playwright ───────────────────────────────────────────────────────────────
    playwright_browser: Literal["chromium", "firefox", "webkit"] = "chromium"
    playwright_headless: bool = True
    playwright_timeout_ms: int = 30_000
    playwright_pool_size: int = 3
    playwright_viewport_width: int = 1280
    playwright_viewport_height: int = 800

    # ── LinkedIn scraper ─────────────────────────────────────────────────────────
    linkedin_scraper_slow_mo_ms:         int = 600    # ms between Playwright actions
    linkedin_description_concurrency:    int = 3      # parallel detail-page tabs
    linkedin_headless:                   bool = True
    linkedin_email:                      str = ""
    linkedin_password:                   str = ""
    # If the LinkedIn account uses Microsoft/Google SSO, set these separately.
    # Leave blank to fall back to linkedin_email / linkedin_password.
    microsoft_email:    str = ""
    microsoft_password: str = ""

    # ── Naukri scraper ────────────────────────────────────────────────────────────
    naukri_email:    str = ""
    naukri_password: str = ""

    # ── Dice scraper (public board — credentials optional) ────────────────────────
    dice_email:    str = ""
    dice_password: str = ""

    # ── Storage ──────────────────────────────────────────────────────────────────
    storage_backend: Literal["local", "s3"] = "local"
    storage_local_dir: str = "./data/results"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    s3_bucket: str = "harvest-results"

    # ── Auth / OTP ───────────────────────────────────────────────────────────────
    allowed_email_domain: str = "sightspectrum.com"
    otp_length: int = 6
    otp_expiry_seconds: int = 300
    otp_max_attempts: int = 5
    otp_resend_cooldown_seconds: int = 60

    # ── JWT ──────────────────────────────────────────────────────────────────────
    jwt_secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30

    # ── SMTP ─────────────────────────────────────────────────────────────────────
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_use_tls: bool = True

    # ── CORS ─────────────────────────────────────────────────────────────────────
    # Kept as a plain str (not list[str]) — pydantic-settings' env source treats
    # list-typed fields as JSON and raises SettingsError on a comma-separated
    # value like "http://a,http://b" before any field_validator ever runs.
    cors_origins: str = "http://localhost:3000,http://localhost:8080"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
