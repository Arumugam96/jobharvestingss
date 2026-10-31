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
    # Full public URL of POST /recruiter-finder/apollo-phone-webhook (Apollo delivers
    # revealed phones there asynchronously). Empty disables phone reveal everywhere.
    apollo_webhook_url: str = ""
    # Shared secret appended to the webhook URL as ?token=… — Apollo doesn't sign its
    # webhook, so the receiver authenticates deliveries by this token instead.
    apollo_webhook_token: str = ""
    # A phone request with no delivery after this long is marked expired (NOT_FOUND);
    # a late delivery still lands and is charged.
    apollo_phone_pending_ttl_minutes: int = 60
    apollo_recheck_days: int = 30
    apollo_enrich_company: bool = True
    # Account-wide ceiling on Apollo credit-calls per UTC day (every caller counts).
    apollo_daily_cap: int = 100
    # Additional sub-cap for harvest/maintenance contact enrichment only (also counts
    # against apollo_daily_cap). When exhausted, enrichment stops but Recruiter Finder
    # can still use the remaining global headroom. <= 0 = no sub-cap.
    apollo_enrichment_daily_cap: int = 0
    # Monthly credit pool (e.g. 2500). Each day's usage subtracts from it;
    # the email report shows remaining. <= 0 = unlimited / not tracked.
    apollo_monthly_cap: int = 0
    company_contact_fallback: bool = True
    company_contact_reveal_cap: int = 5
    # HR shortlist per (company, location) — free Apollo search, revealed lazily as
    # outreach rotates through it (one person per company+location per day).
    company_contact_shortlist_size: int = 10
    # Only shortlist people whose email Apollo marks verified.
    company_contact_verified_only: bool = True
    # Auto-outreach skips a company for this many days after anyone there replied.
    outreach_company_reply_pause_days: int = 30

    # ── Recruiter Contact Finder (app/services/recruiter_finder_service.py) ───────
    recruiter_finder_daily_cap: int = 50
    # Finder credits charged per phone — only when Apollo's webhook actually delivers
    # one (an email stays 1 credit, charged on a hit).
    recruiter_finder_phone_credit_cost: int = 8
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
    # How POST /naukri-recruiter/login signs in (app/services/naukri_launcher.py):
    # "launcher" runs the official Naukri Launcher (Linux AppImage) on the Live
    # Browser display and opens the auto-login URL it hands off in the recruiter
    # profile; "browser" opens recruit.naukri.com in the recruiter profile (blocked
    # for accounts Naukri has moved to Launcher-only login). "auto" = launcher when
    # the AppImage is present on a Linux host, else browser.
    naukri_recruiter_login_mode: Literal["auto", "launcher", "browser"] = "auto"
    # The AppImage (downloaded from Naukri's Linux link) and where it is unpacked.
    # The Launcher's own config (incl. its install id) lives under data/ so it
    # survives image rebuilds.
    naukri_launcher_appimage: str = "data/naukri_launcher/naukri-launcher.AppImage"
    naukri_launcher_install_dir: str = "/opt/naukri-launcher"
    naukri_launcher_config_dir: str = "data/naukri_launcher/config"

    # ---------   Resdex candidate matching (app/services/resdex_match_service.py) ---
    # Before automatic outreach, each recruiter/job-posting-email job (LinkedIn /
    # Naukri) gets a Resdex search in the recruiter profile: JD → LLM requirements
    # → Boolean query + filters → 2-3 candidates, stored and shown on the job
    # detail page. Off by default; RESDEX_INCLUDE_IN_EMAIL additionally puts an
    # anonymised candidate card into the email (keep it off for a shadow period).
    resdex_enabled: bool = False
    resdex_include_in_email: bool = False
    # What RESDEX_INCLUDE_IN_EMAIL puts in the email: "count" = one line such as
    # "we have 20+ matching profiles for your <title> requirement…" (rounded from
    # the newest matched/cap-reached search's Resdex result count, no profiles
    # shown); "cards" = the old anonymised candidate cards. Unknown → "count".
    resdex_email_mode: str = "count"
    # Count mode: below this many Resdex results the line is left out.
    resdex_email_min_count: int = 3
    # "✓ Naukri Verified" badge on EVERY Resdex candidate card (email text + HTML
    # parts, job detail page), whatever the candidate's status. False hides it.
    resdex_show_naukri_verified: bool = True
    resdex_sources: str = "LinkedIn,Naukri"
    # Also search for jobs whose only contact is the Apollo company+location HR
    # contact (no recruiter/job-posting email) — more searches, e.g. while testing.
    resdex_include_company_contacts: bool = False
    # Also search for jobs with NO email at all (no recruiter and no Apollo contact).
    # Candidates are stored for the job detail page only — such jobs are never
    # emailed. Useful while testing, when few harvested jobs carry an email.
    resdex_include_jobs_without_email: bool = False
    # Apollo-enriched contacts: also search for jobs whose only contact is the Apollo
    # company+location HR contact (either this or RESDEX_INCLUDE_COMPANY_CONTACTS
    # enables it), and run the end-of-harvest catch-up sweep for earlier jobs whose
    # Apollo contact arrived after their harvest (Contact Finder, prospect
    # intelligence, Apollo recruiter sweep). Apollo recruiter emails found during a
    # harvest are always eligible, like any recruiter email.
    resdex_include_apollo_contacts: bool = True
    # Catch-up sweep (outreach_sweep_service.run_apollo_resdex_sweep): at most this
    # many jobs per harvest (0 = off), harvested within the last N days.
    resdex_apollo_sweep_max_jobs: int = 10
    resdex_apollo_sweep_lookback_days: int = 7
    resdex_candidates_per_job: int = 3
    resdex_min_skill_coverage: float = 0.6     # share of mandatory skills a pick must show
    resdex_active_within_months: int = 3       # Resdex "active in" filter (0 = off)
    resdex_max_searches_per_job: int = 3       # relaxation ladder T1..T3
    # 1 | 2 | 3 = ONE search per job with only that ladder tier's query + filters
    # (resdex_max_searches_per_job is then ignored); 0 = the full ladder above.
    # The job detail page shows all three tier queries either way.
    resdex_search_tier: int = 0
    # Open the picked candidates' full profiles for richer extraction — spends
    # Resdex profile-view credits, so off by default (search-result cards only).
    resdex_open_profiles: bool = False
    # Hard per-UTC-day caps (DB-enforced, account-wide, fail CLOSED). 0 = unlimited.
    resdex_daily_search_cap: int = 100
    resdex_daily_candidate_cap: int = 100
    resdex_reuse_days: int = 14                # reuse a job_url's recent search instead of re-searching
    resdex_delay_min_ms: int = 5_000          # human-like gap between searches
    resdex_delay_max_ms: int = 20_000
    # Anti-automation pacing (app/agents/resdex_candidate_agent.py): after results
    # load, scroll the list and dwell a random 5-15 s like a recruiter reading it;
    # every N searches in a batch take a longer 2-4 min break (0 = never).
    resdex_results_dwell_min_s: float = 25.0
    resdex_results_dwell_max_s: float = 35.0
    resdex_break_every_searches: int = 20
    resdex_break_min_s: float = 120.0
    resdex_break_max_s: float = 240.0
    # A CAPTCHA / bot wall during a batch is held open in the Live Browser for a
    # human to solve, for at most this long; unsolved → the batch stops.
    resdex_human_wait_s: int = 600
    # Global "login needed" requests (app/services/login_required.py): how long a
    # run that can pause (the Resdex step after an expired recruiter session)
    # waits for a person to log in via the Live Browser before carrying on
    # without that source. 0 = never wait.
    login_wait_s: int = 600
    resdex_llm_timeout_s: float = 45.0
    # Provider for the Resdex LLM passes only ("" = inherit EXTRACTION_LLM_MODEL).
    resdex_llm_model: str = ""

    @property
    def resdex_source_list(self) -> list[str]:
        return [s.strip() for s in self.resdex_sources.split(",") if s.strip()]
    # Per-job detail pass (description HTML, employment type, work mode, industry,
    # company website, recruiter contact when Naukri shows one). Off ⇒ listing-only.
    naukri_fetch_details: bool = True
    # Human-like randomized gap between consecutive Naukri detail-page visits
    # (~45 s average). The adaptive governor multiplies it after each
    # CAPTCHA/block and eases back after a clean streak.
    naukri_detail_delay_min_ms: int = 15_000
    naukri_detail_delay_max_ms: int = 30_000
    # How long a detail page is "read" once loaded (random scrolls down / back
    # up). Spent ON the job page and deducted from the gap above, so the
    # visit rate is unchanged — the time just moves from the SRP to the JD.
    naukri_detail_dwell_min_s: int = 20
    naukri_detail_dwell_max_s: int = 35
    naukri_backoff_max_multiplier: float = 2.0     # cap on the adaptive slow-down
    naukri_backoff_clean_streak: int = 10          # clean detail pages before easing back one step
    # A longer "coffee break" every N detail pages.
    naukri_break_every_details: int = 50           # 0 = never
    naukri_break_min_s: int = 60
    naukri_break_max_s: int = 90
    # On a blocked detail page: cool down, then retry that job once.
    naukri_block_cooldown_min_s: int = 60
    naukri_block_cooldown_max_s: int = 90
    # Circuit breaker: after N consecutive blocks pause details, then probe one.
    naukri_breaker_threshold: int = 3
    naukri_breaker_pause_min_s: int = 600
    naukri_breaker_pause_max_s: int = 900
    # Give up on the run once this many pause-then-probe attempts fail in a row
    # (a hard block) instead of pausing ever longer; collected jobs are kept.
    naukri_max_failed_probes: int = 3
    naukri_human_wait_s: int = 600
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
    # Even/odd day-of-month rotation: "even_day@example.com,odd_day@example.com".
    # Even days (2nd, 4th, …) → first address; odd days (1st, 3rd, …) → second.
    # A per-tenant MAP entry overrides rotation; when unset or only one address,
    # falls through to OUTREACH_REPLY_FORWARD_DEFAULT.
    outreach_reply_forward_rotation: str = ""
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

        Resolution order:
        1. OUTREACH_REPLY_FORWARD_MAP — per-tenant "tenant_id:address" entry.
        2. OUTREACH_REPLY_FORWARD_ROTATION — even/odd day-of-month rotation.
        3. OUTREACH_REPLY_FORWARD_DEFAULT — static fallback.
        Returns "" when none resolve (no forward)."""
        tid = (tenant_id or "").strip()
        for pair in (self.outreach_reply_forward_map or "").split(","):
            if ":" not in pair:
                continue
            key, _, addr = pair.partition(":")
            if key.strip() == tid and addr.strip():
                return addr.strip()

        rotation_addr = self._rotation_forward_address()
        if rotation_addr:
            return rotation_addr

        return (self.outreach_reply_forward_default or "").strip()

    def _rotation_forward_address(self) -> str:
        """Pick the rotation address for today's even/odd day, or "" if rotation
        is not configured (fewer than 2 addresses)."""
        addrs = _split_email_csv(self.outreach_reply_forward_rotation)
        if len(addrs) < 2:
            return ""
        try:
            from app.services.reply_to_rotation import _today
            day = _today().day
        except Exception:
            from datetime import datetime
            day = datetime.now().day
        return addrs[0] if day % 2 == 0 else addrs[1]

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
