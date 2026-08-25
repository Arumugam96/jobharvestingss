"""Application configuration via pydantic-settings."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
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
    data_source: Literal["auto", "database", "json"] = "database"

    # Global daily harvest cap: max jobs that may be scraped across all runs in a
    # UTC day. 0 = unlimited. Enforced at the run start-gate (MAX_JOBS_PER_DAY in
    # .env) — a new run is rejected once today's harvested total reaches this.
    max_jobs_per_day: int = 0

    # ── Database ─────────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://harvest:harvest_password@localhost:5432/harvest_db"
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

    # ── Apollo.io (contact enrichment fallback) ───────────────────────────────────
    # Used only as a last-resort tier: when the LLM/regex LinkedIn extraction returns
    # no email/phone, look the person up on Apollo by their LinkedIn URL. Credit-metered
    # (Basic plan), so calls are cached on the recruiters table and gated. Leave
    # apollo_api_key empty to disable the integration entirely (every call no-ops).
    apollo_api_key: str = ""
    apollo_base_url: str = "https://api.apollo.io/api/v1"
    apollo_timeout_s: float = 20.0
    # Phone reveal is asynchronous on Apollo (delivered via a webhook we don't expose),
    # and costs ~8x an email reveal — off by default; only emails resolve synchronously.
    apollo_reveal_phone: bool = False
    # Per-profile cooldown: don't re-call Apollo for a recruiter enriched/attempted
    # within this many days (prevents re-spending on the same no-match profile).
    apollo_recheck_days: int = 30

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

    naukri_email:    str = ""
    naukri_password: str = ""

    dice_email:    str = ""
    dice_password: str = ""

    # ── Storage ──────────────────────────────────────────────────────────────────
    storage_backend: Literal["local", "s3"] = "local"
    storage_local_dir: str = "./data/results"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    s3_bucket: str = "harvest-results"

    # ── Auth / OTP ───────────────────────────────────────────────────────────────
    # Master switch for login enforcement. Default True (login required). Set
    # AUTH_ENABLED=false in dev to bypass the OTP gate — get_current_user then
    # returns a synthetic dev user so protected routes accept tokenless calls.
    auth_enabled: bool = True
    allowed_email_domain: str = "sightspectrum.com"
    otp_length: int = 6
    otp_expiry_seconds: int = 300
    otp_max_attempts: int = 5
    otp_resend_cooldown_seconds: int = 60

    # ── JWT ──────────────────────────────────────────────────────────────────────
    jwt_secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30

    # ── Persistent session (HttpOnly cookie, survives refresh/restart) ────────────
    # After OTP verification we mint an opaque server-side session and set it as a
    # secure HttpOnly cookie. The session slides: any authenticated request within
    # the lifetime window pushes its expiry forward, so daily users rarely re-OTP.
    session_cookie_name: str = "ha_session"
    session_lifetime_days: int = 30           # sliding window — each use extends expiry by this
    session_renew_interval_minutes: int = 60  # throttle: only extend once per this interval
    # Secure=True means the cookie is only sent over HTTPS (localhost is treated as
    # a secure context by modern browsers, so it still works in dev). Set
    # SESSION_COOKIE_SECURE=false only if you serve the app over plain HTTP on a
    # non-localhost host. SameSite=lax is correct for the same-origin (nginx) prod
    # deploy and the same-site localhost dev setup; use "none" only for a truly
    # cross-site frontend (which then also requires Secure=true).
    session_cookie_secure: bool = True
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    session_cookie_domain: str = ""           # empty → host-only cookie

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
        return [o for o in self.cors_origins.split(",") if o.strip()]

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
