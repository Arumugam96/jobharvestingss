"""Application configuration via pydantic-settings."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
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
    # UTC day. 0 = unlimited. Enforced at the run start-gate
    max_jobs_per_day: int = 0
    harvest_persist_batch_size: int = 10

    # ── Sales Navigator (standalone experimental script) ─────────────────────────
    sales_navigator_max_jobs_per_day: int = 0
    sales_navigator_daily_stop_ceiling: int = 0

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
    # Single failover provider tried when the primary extraction provider is down
    # (same value format as EXTRACTION_LLM_MODEL, e.g. "claude" | "openrouter").
    # "" disables failover — the run degrades straight to re-enrichment instead.
    extraction_fallback_model: str = ""
    local_llm_url:        str = "http://localhost:11434"
    local_llm_model:      str = "llama3.1:8b"

    # ── Re-enrichment (degraded jobs when all LLM providers were down) ─────────────
    # Jobs stored for re-enrichment are retried at the start of each harvest run
    # for this many days; past that they're marked permanently failed.
    reenrichment_max_age_days: int = 7
    # Cap on how many pending jobs one start-of-run sweep re-extracts, so the
    # sweep never delays a harvest's start by much.
    reenrichment_sweep_limit: int = 50

    # ── OpenRouter (fallback LLM for HTML extraction) ──────────────────────────────
    openrouter_api_key: str = ""
    openrouter_model:   str = "google/gemma-4-26b-a4b-it:free"

    # ── Google Gemini ────────────────────────────────────────────────────────────
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    gemini_max_output_tokens: int = 2048
    gemini_temperature: float = 0.0

    apollo_api_key: str = ""
    apollo_base_url: str = "https://api.apollo.io/api/v1"
    apollo_timeout_s: float = 20.0
    apollo_reveal_phone: bool = False
    apollo_webhook_url: str = ""
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

    # ── LinkedIn Home Feed lead harvest ──────────────────────────────────────────
    # Scrapes the authenticated recruiter's Home Feed (/feed/), NOT the Jobs board.
    # All stop conditions are env-tunable so a run can be bounded without a redeploy.
    linkedin_feed_max_posts:             int = 10      # max posts to inspect, then stop
    linkedin_feed_max_scrolls:           int = 40      # max scroll actions, then stop
    linkedin_feed_scroll_delay_ms:       int = 2500    # pause between scrolls (lazy-load)
    # Always perform at least this many scrolls before honouring the "no new posts"
    # early-stop below. IT hiring posts sit deep in the Home Feed and LinkedIn often
    # serves a barren batch before loading more, so bailing after the first few quiet
    # scrolls would miss them entirely.
    linkedin_feed_min_scrolls:           int = 10
    # Stop after this many consecutive scrolls that surface no NEW (non-duplicate)
    # posts — the feed keeps rendering the same items (natural end of fresh content).
    linkedin_feed_no_new_stop_rounds:    int = 3
    # Stop after this many consecutive scrolls where the feed DOM yields ZERO post
    # containers — a distinct signal from "no new posts": feed exhausted or a DOM
    # change broke container matching. Ends the run instead of scrolling forever.
    linkedin_feed_empty_dom_stop_rounds: int = 2
    # Per-run cap on LLM calls (classify + extract combined) — bounds cost even if
    # the candidate filter lets a lot of posts through.
    linkedin_feed_llm_max_calls:         int = 300
    # Minimum classifier confidence for a post to count as a genuine IT hiring lead.
    linkedin_feed_min_confidence:        float = 0.6

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
    auth_enabled: bool = True
    allowed_email_domain: str = "sightspectrum"
    otp_length: int = 6
    otp_expiry_seconds: int = 300
    otp_max_attempts: int = 5
    otp_resend_cooldown_seconds: int = 60

    # ── JWT ──────────────────────────────────────────────────────────────────────
    jwt_secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30

    # ── Persistent session (HttpOnly cookie, survives refresh/restart) ────────────
    session_cookie_name: str = "ha_session"
    session_lifetime_days: int = 30           
    session_renew_interval_minutes: int = 60  
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
    smtp_timeout_seconds: int = 60
    outreach_deck_url: str = ""

    # ── Mailjet (transactional email transport — replaces the SMTP send path) ─────
    # All mail (OTP, outreach, harvest report) is delivered via the Mailjet Send API
    # v3.1. Credentials accept the Mailjet-native env names used by check_mailjet.py
    # (MJ_APIKEY_PUBLIC / MJ_APIKEY_PRIVATE) as well as MAILJET_* aliases.
    mailjet_api_key: str = Field(default="", validation_alias=AliasChoices("MJ_APIKEY_PUBLIC", "MAILJET_API_KEY"))
    mailjet_secret_key: str = Field(default="", validation_alias=AliasChoices("MJ_APIKEY_PRIVATE", "MAILJET_SECRET_KEY"))
    mailjet_timeout_seconds: int = 30
    # Shared secret embedded in the Mailjet event-webhook URL (?token=…) so only
    # Mailjet's delivery-event callbacks are accepted. Empty disables the check.
    mailjet_webhook_token: str = ""
    # Public base URL of the app (scheme + host, no trailing slash), e.g.
    # "https://app.example.com" — used to build absolute links in outreach emails
    # (the unsubscribe link + List-Unsubscribe header). Falls back to localhost.
    public_base_url: str = ""

    # ── CORS ─────────────────────────────────────────────────────────────────────
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
