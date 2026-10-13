"""Application configuration via pydantic-settings."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_email_csv(value: str | None) -> list[str]:
    """Parse a comma-separated address list: strip each, drop empties, and
    de-dupe case-insensitively while preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for addr in (value or "").split(","):
        a = addr.strip()
        key = a.lower()
        if a and key not in seen:
            seen.add(key)
            out.append(a)
    return out


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

    log_level: str = "INFO"
    data_source: Literal["auto", "database", "json"] = "database"
    max_jobs_per_day: int = 0
    harvest_persist_batch_size: int = 10
    harvest_dedup_at_source: bool = True
    harvest_skip_already_harvested_days: int = 30

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

    # ── Local LLM provider ──────────────────────────────────────────────
    extraction_llm_model: str = "claude"
    extraction_fallback_model: str = ""
    local_llm_url:        str = "http://localhost:11434"
    local_llm_model:      str = "llama3.1:8b"

    # ── OpenRouter (fallback LLM for HTML extraction) ──────────────────────────────
    openrouter_api_key: str = ""
    openrouter_model:   str = "google/gemma-4-26b-a4b-it:free"
    
    # ── Re-enrichment (degraded jobs when all LLM providers were down) ─────────────
    reenrichment_max_age_days: int = 7
    reenrichment_sweep_limit: int = 50

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
    apollo_enrich_company: bool = True
    apollo_daily_cap: int = 100
    company_contact_fallback: bool = True
    company_contact_reveal_cap: int = 5

    # ── Recruiter Contact Finder (app/services/recruiter_finder_service.py) ───────
    recruiter_finder_daily_cap: int = 50
    recruiter_finder_max_rows: int = 1000
    company_contact_max_count: int = 25
    recruiter_finder_concurrency: int = 3

    # ── Playwright ───────────────────────────────────────────────────────────────
    playwright_browser: Literal["chromium", "firefox", "webkit"] = "chromium"
    playwright_headless: bool = True
    playwright_timeout_ms: int = 30_000
    playwright_pool_size: int = 3
    playwright_viewport_width: int = 1280
    playwright_viewport_height: int = 800

    # ── LinkedIn scraper ─────────────────────────────────────────────────────────
    linkedin_contact_scraping:           bool = False
    linkedin_scraper_slow_mo_ms:         int = 600    # ms between Playwright actions
    linkedin_description_concurrency:    int = 3      # parallel detail-page tabs
    linkedin_headless:                   bool = True
    linkedin_email:                      str = ""
    linkedin_password:                   str = ""
    # If the LinkedIn account uses Microsoft/Google SSO, set these separately.
    # Leave blank to fall back to linkedin_email / linkedin_password.
    microsoft_email:    str = ""
    microsoft_password: str = ""

    # ── LinkedIn profile-visit anti-detection ────────────────────────────────────
    linkedin_profile_visit_delay_min_ms: int = 8_000
    linkedin_profile_visit_delay_max_ms: int = 25_000
    linkedin_recruiter_visit_cap: int = 25
    prospect_intelligence_enabled: bool = False

    # ── LinkedIn Home Feed lead harvest ──────────────────────────────────────────
    linkedin_feed_max_posts:             int = 10     
    linkedin_feed_max_scrolls:           int = 40      
    linkedin_feed_scroll_delay_ms:       int = 2500    # pause between scrolls (lazy-load)
    linkedin_feed_min_scrolls:           int = 10
    linkedin_feed_no_new_stop_rounds:    int = 3
    linkedin_feed_empty_dom_stop_rounds: int = 2
    linkedin_feed_llm_max_calls:         int = 300
    linkedin_feed_min_confidence:        float = 0.6    # condifence level of llm to classify

    # ---------   Naukri Configurations     -------------------
    naukri_email:    str = ""
    naukri_password: str = ""
    # "shared" (default): the unified harvest runs ONE browser (the LinkedIn
    # account's persistent profile, full bundled Chromium) with LinkedIn and
    # Naukri as two concurrent tabs. "isolated": Naukri gets its own fresh
    # cookie-less guest browser (Naukri-only worker / host without the profile).
    # "recruiter": Naukri gets its own browser on the logged-in RECRUITER profile
    # (naukri_recruiter_profile_dir) — www.naukri.com job pages browsed with the
    # recruiter session; the run checks the session first and refuses to fall
    # back to a guest silently.
    naukri_browser_mode: Literal["shared", "isolated", "recruiter"] = "shared"
    # Dedicated persistent Chrome profile for the Naukri RECRUITER session
    # (app/agents/naukri_recruiter_agent.py). Deliberately separate from the
    # LinkedIn/harvest profile so recruiter cookies never reach the guest
    # Naukri job-harvest tab. Relative paths anchor at the ai-harvest-agent root.
    naukri_recruiter_profile_dir: str = "data/naukri_recruiter_profile"
    # Per-job detail pass (description HTML, employment type, work mode, industry,
    # company website, recruiter contact when Naukri shows one). Off ⇒ listing-only.
    naukri_fetch_details: bool = True
    # Human-like randomized gap between consecutive Naukri detail-page visits
    # (~45 s average). The adaptive governor multiplies it after each
    # CAPTCHA/block and eases back after a clean streak.
    naukri_detail_delay_min_ms: int = 30_000
    naukri_detail_delay_max_ms: int = 60_000
    naukri_backoff_max_multiplier: float = 4.0     # cap on the adaptive slow-down
    naukri_backoff_clean_streak: int = 10          # clean detail pages before easing back one step
    # A longer "coffee break" every N detail pages.
    naukri_break_every_details: int = 18           # 0 = never
    naukri_break_min_s: int = 180
    naukri_break_max_s: int = 300
    # On a blocked detail page: cool down, then retry that job once.
    naukri_block_cooldown_min_s: int = 120
    naukri_block_cooldown_max_s: int = 300
    # Circuit breaker: after N consecutive blocks pause details, then probe one.
    naukri_breaker_threshold: int = 3
    naukri_breaker_pause_min_s: int = 600
    naukri_breaker_pause_max_s: int = 900
    # Human-in-the-loop: when blocks persist on a manual run, wait this long for
    # someone to solve the CAPTCHA in the Live Browser (0 = never wait).
    naukri_human_wait_s: int = 600
    # LLM enrichment on top of the deterministic parse (app/services/jd_llm_enrichment.py),
    # via the configured EXTRACTION_LLM_MODEL chain. Every LLM contact is grounded
    # against the description text (never stored unless written there); a
    # restructured description is kept only if no word changed. Skipped with one
    # warning when no LLM provider is configured; an LLM error never loses a job.
    #   contact extraction — only for descriptions showing a contact signal
    #   restructure        — only for flat, run-on descriptions
    naukri_llm_contact_extraction: bool = True
    naukri_llm_description_restructure: bool = True
    naukri_llm_timeout_s: float = 45.0        # per LLM call
    naukri_llm_concurrency: int = 2           # LLM calls in flight alongside the detail loop
    naukri_llm_max_calls_per_run: int = 1000  # cost backstop (0 = unlimited)
    # Provider for THIS pass only (same strings as EXTRACTION_LLM_MODEL, e.g.
    # "claude", "claude-sonnet-…", "ollama", "openrouter/<id>"). Empty = inherit
    # EXTRACTION_LLM_MODEL / EXTRACTION_FALLBACK_MODEL. Lets contact accuracy use
    # a stronger model while the rest of the app stays on a local LLM.
    naukri_llm_model: str = ""
    naukri_llm_fallback_model: str = ""

    # ---------------       DICE Configurations     -------------
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
    # Secure=True means the cookie is only sent over HTTPS 
    session_cookie_secure: bool = True
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    session_cookie_domain: str = ""           # empty → host-only cookie

    # ── Email transport selection ─────────────────────────────────────────────────
    email_provider: Literal["mailjet", "smtp"] = "smtp"

    # ── SMTP ─────────────────────────────────────────────────────────────────────
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_use_tls: bool = True
    smtp_timeout_seconds: int = 60
    smtp_sender_mail: str = ""
    smtp_envelope_name: str = ""
    otp_from_email: str = "no-reply@sightspectrum.com"
    otp_from_name: str = "SightSpectrum Login OTP"
    outreach_deck_url: str = ""

    # ── Mailjet (transactional email transport — replaces the SMTP send path) ─────
    # mailjet_api_key: str = Field(default="", validation_alias=AliasChoices("MJ_APIKEY_PUBLIC", "MAILJET_API_KEY"))
    # mailjet_secret_key: str = Field(default="", validation_alias=AliasChoices("MJ_APIKEY_PRIVATE", "MAILJET_SECRET_KEY"))
    # mailjet_timeout_seconds: int = 30
    mailjet_max_attempts: int = 2
    mailjet_retry_backoff_seconds: float = 0.5
    # mailjet_webhook_token: str = ""
    brevo_webhook_token: str = ""
    # Shared secret guarding the Brevo INBOUND-parse webhook (prospect replies), in the
    # URL query like the event webhooks. Falls back to brevo_webhook_token when unset.
    brevo_inbound_token: str = ""
    # Per-tenant mailbox each captured reply is forwarded to (the email "alarm").
    # Comma-separated "tenant_id:address" pairs, e.g.
    #   "internal:replies@sightspectrum.com,client_us:us-replies@…,client_in:in-replies@…"
    # A tenant with no entry falls back to OUTREACH_REPLY_FORWARD_DEFAULT; with neither,
    # forwarding is skipped (the reply is still captured + shown in-app).
    outreach_reply_forward_map: str = ""
    outreach_reply_forward_default: str = ""
    # Brevo v3 API key (header "api-key"). Used ONLY by scripts/register_brevo_events.py to
    # create/update the transactional event webhook — NOT the SMTP key used to send mail.
    brevo_api_key: str = ""

    # ── Reply tracking (inbound prospect replies) ─────────────────────────────────
    reply_tracking_mode: Literal["brevo", "imap", "graph", "off"] = "brevo"

    imap_host: str = ""                 # e.g. imap.gmail.com, imappro.zoho.com, outlook.office365.com
    imap_port: int = 993
    imap_username: str = ""             # the reply mailbox address
    imap_password: str = ""
    imap_use_ssl: bool = True           # True → implicit TLS on 993 (standard); False → plain/STARTTLS on 143
    imap_mailbox: str = "INBOX"         # folder to scan — point at a dedicated "Replies" folder if a server-side filter files them there
    imap_poll_interval_seconds: int = 180
    imap_max_messages_per_poll: int = 50

    # ── Microsoft Graph reply mailbox (used only when REPLY_TRACKING_MODE="graph") ─
    graph_tenant_id: str = ""        # Entra "Directory (tenant) ID"
    graph_client_id: str = ""        # Entra "Application (client) ID"
    graph_client_secret: str = ""    # a client secret VALUE (not the secret id)
    # the OUTREACH_AUTO_REPLY_TO / Reply-To address. Blank falls back to SMTP_SENDER_MAIL.
    graph_mailbox: str = ""
    graph_mailbox_folder: str = "inbox"
    graph_poll_interval_seconds: int = 1800
    graph_max_messages_per_poll: int = 50
    
    # ── Automated end-of-harvest outreach ────────────────────────────────────────
    public_base_url: str = ""
    outreach_track_engagement: bool = True
    outreach_auto_send_on_harvest: bool = True
    outreach_auto_reply_to: str = ""
    outreach_extra_reply_to: str = ""

    # ── CORS ─────────────────────────────────────────────────────────────────────
    cors_origins: str = "http://localhost:3000,http://localhost:8080"

    
    @property
    def cors_origins_list(self) -> list[str]:
        return [o for o in self.cors_origins.split(",") if o.strip()]

    @property
    def outreach_extra_reply_to_recipients(self) -> list[str]:
        """Extra Reply-To addresses for outreach, parsed from the comma-separated
        OUTREACH_EXTRA_REPLY_TO. Stripped, empties dropped, order-preserving
        de-dupe. Empty list when unset (no extra Reply-To added)."""
        return _split_email_csv(self.outreach_extra_reply_to)

    @property
    def outreach_auto_reply_to_recipients(self) -> list[str]:
        """Auto-outreach Reply-To addresses, parsed from the comma-separated
        OUTREACH_AUTO_REPLY_TO. Stripped, empties dropped, order-preserving
        de-dupe. Order matters: when two or more are configured, the auto-outreach
        flow rotates the *primary* Reply-To through this list one address per
        calendar day (day 1 → index 0, day 2 → index 1, …). Empty list when unset."""
        return _split_email_csv(self.outreach_auto_reply_to)

    def reply_forward_address(self, tenant_id: str | None) -> str:
        """The mailbox a captured reply should be forwarded to for ``tenant_id``.

        Parses OUTREACH_REPLY_FORWARD_MAP ("tenant_id:address" pairs) and returns
        the tenant's address, else OUTREACH_REPLY_FORWARD_DEFAULT, else "" (no
        forward). Robust to spaces around pairs/colons and empty entries."""
        tid = (tenant_id or "").strip()
        for pair in (self.outreach_reply_forward_map or "").split(","):
            if ":" not in pair:
                continue
            key, _, addr = pair.partition(":")
            if key.strip() == tid and addr.strip():
                return addr.strip()
        return (self.outreach_reply_forward_default or "").strip()

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
