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
    extraction_fallback_model: str = ""
    local_llm_url:        str = "http://localhost:11434"
    local_llm_model:      str = "llama3.1:8b"

    # ── Re-enrichment (degraded jobs when all LLM providers were down) ─────────────
    reenrichment_max_age_days: int = 7
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
    # Company-enrichment Apollo stage: the LinkedIn company-enrichment waterfall
    # (LinkedIn company page → Apollo → company website → LLM) spends a credit on
    # POST /organizations/enrich. On by default; the per-company
    # 30-day recheck cooldown (apollo_recheck_days) prevents re-billing.
    apollo_enrich_company: bool = True

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

    # ── Email transport selection ─────────────────────────────────────────────────
    # Which transport EmailSender uses for ALL mail (OTP, outreach, harvest report):
    #   "mailjet" → the Mailjet Send API v3.1 (default; keeps current behavior)
    #   "smtp"    → a plain SMTP relay using the SMTP_* fields below. Set this to route
    #               mail through Brevo (smtp-relay.brevo.com:587, STARTTLS) with the SMTP
    #               login + SMTP key; Brevo tracks opens/clicks itself and echoes our row
    #               id back on its webhooks via the X-Mailin-custom header.
    email_provider: Literal["mailjet", "smtp"] = "mailjet"

    # ── SMTP ─────────────────────────────────────────────────────────────────────
    # Used both to derive the From/identity AND, when email_provider="smtp", as the live
    # transport (Brevo relay: host=smtp-relay.brevo.com, port=587, use_tls=True,
    # username=<SMTP login>, password=<SMTP key — NOT the account password / API key>).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_use_tls: bool = True
    smtp_timeout_seconds: int = 60
    # Explicit outgoing sender identity — preferred over smtp_from_email/smtp_username for
    # the visible From on ALL mail (OTP, outreach, harvest report). REQUIRED for Brevo,
    # whose SMTP login (smtp_username) is NOT a valid From: mail must come from a
    # Brevo-verified sender address.
    #   smtp_sender_mail    → the From email address (SMTP_SENDER_MAIL) — a verified sender
    #   smtp_envelope_name  → the From display name (SMTP_ENVELOPE_NAME), e.g. "JOB HARVEST AGENT"
    smtp_sender_mail: str = ""
    smtp_envelope_name: str = ""
    outreach_deck_url: str = ""

    # ── Mailjet (transactional email transport — replaces the SMTP send path) ─────
    # All mail (OTP, outreach, harvest report) is delivered via the Mailjet Send API
    # v3.1. Credentials accept the Mailjet-native env names used by check_mailjet.py
    # (MJ_APIKEY_PUBLIC / MJ_APIKEY_PRIVATE) as well as MAILJET_* aliases.
    mailjet_api_key: str = Field(default="", validation_alias=AliasChoices("MJ_APIKEY_PUBLIC", "MAILJET_API_KEY"))
    mailjet_secret_key: str = Field(default="", validation_alias=AliasChoices("MJ_APIKEY_PRIVATE", "MAILJET_SECRET_KEY"))
    mailjet_timeout_seconds: int = 30
    # Transient-failure retry for the Mailjet Send API. A single flaky TLS handshake
    # (ConnectError/BrokenResourceError) or a 429/5xx used to PERMANENTLY fail a send
    # (status="failed", empty error); now each send is retried up to
    # mailjet_max_attempts times with exponential backoff — mailjet_retry_backoff_seconds
    # doubling each attempt (0.5s → 1s → 2s) — on connect/read/write/timeout errors and
    # retryable HTTP statuses (429, 500, 502, 503, 504). Set attempts to 1 to disable.
    mailjet_max_attempts: int = 2
    mailjet_retry_backoff_seconds: float = 0.5
    # Shared secret embedded in the Mailjet event-webhook URL (?token=…) so only
    # Mailjet's delivery-event callbacks are accepted. Empty disables the check.
    mailjet_webhook_token: str = ""
    # Shared secret in the Brevo event-webhook URL (/outreach/brevo-events?token=…) so
    # only Brevo's transactional callbacks are accepted. Empty disables the check.
    brevo_webhook_token: str = ""
    # Brevo v3 API key (header "api-key"). Used ONLY by scripts/register_brevo_events.py to
    # create/update the transactional event webhook — NOT the SMTP key used to send mail.
    brevo_api_key: str = ""
    # Public base URL of the app (scheme + host, no trailing slash), e.g.
    # "https://app.example.com" — used to build absolute links in outreach emails
    # (the unsubscribe link + List-Unsubscribe header). Falls back to localhost.
    public_base_url: str = ""
    # Outreach open/click tracking. When True, outreach sends set Mailjet's
    # TrackOpens/TrackClicks (adds a tracking pixel + rewrites links through Mailjet's
    # redirector). Those are classic "bulk/marketing" signals that push Gmail to the
    # Promotions tab, so this defaults to OFF for 1:1 recruiter outreach. Delivery
    # events (sent/bounce/blocked/spam/unsub) still fire regardless — only open/click
    # analytics are lost when disabled.
    outreach_track_engagement: bool = True

    # ── Automated end-of-harvest outreach ────────────────────────────────────────
    # When True (default), a completed harvest run automatically sends an initial
    # outreach email to every harvested job's recruiter that has a resolvable email,
    # reusing the same generation → Mailjet → email_outreach logging as a manual send
    # (each auto-send shows up in the Mail-logs UI). Suppressed/unsubscribed/already-
    # contacted recruiters are skipped, so it is idempotent across runs. Set False to
    # disable the auto-send entirely.
    outreach_auto_send_on_harvest: bool = True
    # Reply-To used for unattended auto-sends (there is no logged-in salesperson). The
    # visible From stays the shared harvest-agent identity; recruiter replies go here,
    # and this address is recorded as the send's `sent_by`. Falls back to
    # SMTP_FROM_EMAIL when empty.
    outreach_auto_reply_to: str = ""

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
