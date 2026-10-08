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

    # Console log verbosity (DEBUG/INFO/WARNING/ERROR). The rotating debug
    # file at data/logs/app.log always captures DEBUG regardless of this —
    # this only controls what prints to the terminal.
    log_level: str = "INFO"
    data_source: Literal["auto", "database", "json"] = "database"
    max_jobs_per_day: int = 0
    harvest_persist_batch_size: int = 10
    # Source-level cross-run dedup (LinkedIn): skip re-scraping a posting whose
    # job_url was already harvested within this many days, so it never re-enters
    # the pipeline (no re-extract, no re-email). 0 = all-time. Disabled entirely
    # when harvest_dedup_at_source is False.
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
    # Hard global ceiling on Apollo credit-spending calls per UTC day, enforced
    # atomically in the DB (app/services/apollo_budget.py) at the ApolloClient
    # chokepoint across ALL runs/processes — a safety backstop so credits can never
    # be overspent regardless of how many harvests/sweeps fire. 0 = unlimited.
    apollo_daily_cap: int = 100
    # Company+location HR-contact fallback: when a harvested job has no resolvable
    # recruiter email, discover the best location-specific HR/recruiting contact for
    # the job's company + location via Apollo (company → org → HR people → email) and
    # reach out to them. Gated here AND on apollo_api_key. See
    # app/services/company_location_contact_service.py.
    company_contact_fallback: bool = True
    # Per (company, location): how many title-ranked HR people to reveal an email for
    # before giving up on finding one (credit conservation — reveals cost credits).
    company_contact_reveal_cap: int = 5

    # ── Recruiter Contact Finder (app/services/recruiter_finder_service.py) ───────
    # The Contact Finder's OWN Apollo credit bucket — one per tenant, per UTC day —
    # kept SEPARATE from apollo_daily_cap (the account-wide harvest backstop) so a
    # workspace's on-demand lookups can't be starved by harvest runs and vice versa.
    # Enforced atomically in the DB (app/services/recruiter_finder_budget.py) keyed
    # on (usage_date, tenant_id). 0 = unlimited. A tenant may override this via its
    # tenants.config JSON ("recruiter_finder_daily_cap"), resolved per request.
    recruiter_finder_daily_cap: int = 50
    # Hard ceiling on rows accepted from one uploaded CSV/XLSX (guards memory + spend).
    recruiter_finder_max_rows: int = 1000
    # Single search (company mode) only: the largest "how many contacts" a user may
    # request in one lookup. Caps per-search credit spend — each revealed contact costs
    # 1 credit (email/phone) or 2 (both), still bounded by the per-tenant daily cap.
    company_contact_max_count: int = 25
    # How many Apollo lookups a single bulk-enrichment job runs in parallel. Apollo
    # is HTTP-only (no browser), so this is just connection concurrency, not the
    # Chrome single-flight guard.
    recruiter_finder_concurrency: int = 3

    # ── Playwright ───────────────────────────────────────────────────────────────
    playwright_browser: Literal["chromium", "firefox", "webkit"] = "chromium"
    playwright_headless: bool = True
    playwright_timeout_ms: int = 30_000
    playwright_pool_size: int = 3
    playwright_viewport_width: int = 1280
    playwright_viewport_height: int = 800

    # ── LinkedIn scraper ─────────────────────────────────────────────────────────
    # Master switch for visiting a person's LinkedIn /in/ profile to scrape
    # publicly visible contact info (email/phone/headline/location). OFF by
    # default. When off, the shared _extract_linkedin_contact_info no-ops, so all
    # three contact-discovery call sites (harvest-time recruiter pass in
    # linkedin_agent, recruiter_contact_agent, prospect_intelligence_agent) skip
    # the profile visit entirely and lean on other sources (e.g. Apollo). Gating
    # in the one shared function keeps the three call sites in sync.
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
    # Human-like randomized pause (ms) inserted BETWEEN recruiter /in/ profile
    # visits during the post-harvest contact-discovery pass (and between prospect
    # lookups). Back-to-back profile opens are a strong bot signal and were a
    # cause of the account being restricted for "high volume of profile data".
    linkedin_profile_visit_delay_min_ms: int = 8_000
    linkedin_profile_visit_delay_max_ms: int = 25_000
    # Max NEW recruiter /in/ profile pages opened per harvest run. Already-visited
    # recruiters are never re-opened (see _enrich_recruiters), so this only bounds
    # first-time visits.
    linkedin_recruiter_visit_cap: int = 25
    # Master switch for the manual Prospect Intelligence tool
    # (POST /run-prospect-intelligence). Off by default — it visits /in/ profiles
    # and is not part of the normal harvest pipeline.
    prospect_intelligence_enabled: bool = False

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
    email_provider: Literal["mailjet", "smtp"] = "smtp"

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
    smtp_sender_mail: str = ""
    smtp_envelope_name: str = ""
    # OTP (login) email sender — its own From identity so the login mail doesn't
    # share the outreach/report one. The address must be a provider-verified sender
    # (any mailbox on the Brevo-authenticated sightspectrum.com domain works). A
    # blank OTP_FROM_EMAIL falls back to the shared identity.
    otp_from_email: str = "no-reply@sightspectrum.com"
    otp_from_name: str = "SightSpectrum Login OTP"
    outreach_deck_url: str = ""

    # ── Mailjet (transactional email transport — replaces the SMTP send path) ─────
    mailjet_api_key: str = Field(default="", validation_alias=AliasChoices("MJ_APIKEY_PUBLIC", "MAILJET_API_KEY"))
    mailjet_secret_key: str = Field(default="", validation_alias=AliasChoices("MJ_APIKEY_PRIVATE", "MAILJET_SECRET_KEY"))
    mailjet_timeout_seconds: int = 30
    mailjet_max_attempts: int = 2
    mailjet_retry_backoff_seconds: float = 0.5
    mailjet_webhook_token: str = ""
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
    # How captured replies reach us. Both paths funnel into the SAME capture pipeline
    # (outreach_reply_service.record_inbound_replies) — matching, storage, replied_at
    # stamping, and the forward-to-alert-mailbox are identical. Only the ingestion
    # SOURCE differs:
    #   "brevo" → Brevo inbound-parse POSTs to /outreach/inbound-reply (needs a
    #             dedicated inbound MX record pointed at Brevo — the legacy default).
    #   "imap"  → we POLL the reply mailbox over IMAP on a timer and feed each new
    #             message through the same pipeline. Needs NO DNS/MX change — it just
    #             reads a mailbox you already own. Use this when the domain can't take
    #             another MX record AND the mailbox provider allows app-password IMAP
    #             (Zoho, Google Workspace, cPanel — NOT Microsoft 365).
    #   "graph" → POLL a Microsoft 365 mailbox via the Microsoft Graph API (app-only
    #             OAuth2). The required path for M365, which blocks password/app-password
    #             IMAP (Basic Auth is disabled). Same pipeline, no DNS/MX change. Uses the
    #             GRAPH_* fields below.
    #   "off"   → none run (replies are not captured).
    # The /outreach/inbound-reply webhook stays mounted in every mode (harmless unless
    # Brevo actually posts to it); this switch only gates which poller runs.
    reply_tracking_mode: Literal["brevo", "imap", "graph", "off"] = "brevo"

    # ── IMAP reply mailbox (used only when REPLY_TRACKING_MODE="imap") ─────────────
    # The mailbox replies land in — i.e. the address used as the outreach From /
    # Reply-To. This is a mailbox on YOUR mail provider (Google Workspace, Zoho, M365,
    # cPanel…), NOT Brevo: Brevo only relays outbound. Every such provider exposes IMAP
    # over TLS on 993 at no extra cost.
    imap_host: str = ""                 # e.g. imap.gmail.com, imappro.zoho.com, outlook.office365.com
    imap_port: int = 993
    imap_username: str = ""             # the reply mailbox address
    # Mailbox password OR an app-specific password. Google Workspace / M365 with MFA
    # require an app password (or OAuth); Zoho / cPanel usually accept the mailbox
    # password directly. See docs/README for the per-provider steps.
    imap_password: str = ""
    imap_use_ssl: bool = True           # True → implicit TLS on 993 (standard); False → plain/STARTTLS on 143
    imap_mailbox: str = "INBOX"         # folder to scan — point at a dedicated "Replies" folder if a server-side filter files them there
    # How often the poller checks for new replies. 120–300s is plenty and well within
    # every provider's IMAP limits.
    imap_poll_interval_seconds: int = 180
    # Safety cap on messages processed per poll (a huge backlog on first run won't
    # stall the tick). Remaining messages are picked up on the next poll.
    imap_max_messages_per_poll: int = 50

    # ── Microsoft Graph reply mailbox (used only when REPLY_TRACKING_MODE="graph") ─
    # App-only (client-credentials) OAuth2 against Microsoft 365. Register an app in
    # Microsoft Entra ID, grant it the APPLICATION permission "Mail.Read" (admin
    # consent), create a client secret, then scope it to just the reply mailbox with
    # an Exchange Application Access Policy (New-ApplicationAccessPolicy). See the
    # README/.env.example for the exact steps. No interactive login, no refresh token.
    graph_tenant_id: str = ""        # Entra "Directory (tenant) ID"
    graph_client_id: str = ""        # Entra "Application (client) ID"
    graph_client_secret: str = ""    # a client secret VALUE (not the secret id)
    # The mailbox whose replies we read, e.g. "ananyamehta@sightspectrum.com" — this is
    # the OUTREACH_AUTO_REPLY_TO / Reply-To address. Blank falls back to SMTP_SENDER_MAIL.
    graph_mailbox: str = ""
    # Which folder to read. "inbox" is the well-known name; a custom folder would be its
    # folder id. Point at a dedicated "Replies" folder if a mail rule files them there.
    graph_mailbox_folder: str = "inbox"
    graph_poll_interval_seconds: int = 180
    graph_max_messages_per_poll: int = 50
    # Public base URL of the app (scheme + host, no trailing slash), e.g.
    # "https://app.example.com" — used to build absolute links in outreach emails
    # (the unsubscribe link + List-Unsubscribe header). Falls back to localhost.
    public_base_url: str = ""
    outreach_track_engagement: bool = True

    # ── Automated end-of-harvest outreach ────────────────────────────────────────
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
