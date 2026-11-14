"""
Pydantic models for the Harvest Agent configuration.

HarvestConfig  ←  loaded from / saved to  data/config/harvest_config.json

All search and business-filter parameters live here.
The backend NEVER hardcodes any of these values — it reads them exclusively
from the saved config that the UI Rule Engine writes.
"""
from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

import structlog
from pydantic import BaseModel, Field, field_validator, model_validator

logger = structlog.get_logger(__name__)

# hiring_entity values that are, by definition, NOT a GCC. When hiring_entity is
# one of these, gcc_mode="gcc_only" is impossible (the intersection is empty);
# and hiring_entity="GCC" with gcc_mode="exclude_gcc" is the symmetric conflict.
_NON_GCC_ENTITIES = {"Direct Client", "Staffing Firm", "Ambiguous"}

# Naukri's left-panel "Company type" options (its business_size cluster labels),
# in the order the Rule Engine UI shows them. Mapped to Naukri's qbusinessSize ids
# in app/agents/naukri_agent.py (_COMPANY_TYPE_MAP).
NAUKRI_COMPANY_TYPES: tuple[str, ...] = (
    "Indian MNC", "Foreign MNC", "Corporate", "Startup", "MNC", "Govt/PSU", "Others",
)

# LinkedIn account ids the harvest may run under (the Rule Engine's Account 1 / 2;
# same set POST /linkedin-setup-session accepts). The id is joined into the Chrome
# profile dir and the session filename, so anything else is rejected.
LINKEDIN_ACCOUNTS: tuple[str, ...] = ("1", "2")
# Legacy spellings of the default account (session_manager._DEFAULT_ACCOUNTS) → "1".
_DEFAULT_ACCOUNT_ALIASES = {"", "default", "primary"}

# The project data dir (ai-harvest-agent/data — /app/data in the container), the
# same anchor ConfigService resolves chrome_profile against.
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def chrome_profile_problem(value: str) -> str | None:
    """Why `value` is not an acceptable browser.chrome_profile, or None if it is.

    Rejects any ".." segment, and a same-OS absolute path (or a Windows
    drive-relative one) that is not strictly inside the project data dir. A
    relative path is anchored under the project root by ConfigService; an
    absolute path for the OTHER OS (e.g. "/app/data/..." seen on Windows) is
    normalized by ConfigService to data/<basename>, so both are allowed."""
    raw = value or ""
    if "\x00" in raw:
        return "chrome_profile must not contain NUL characters"
    if ".." in re.split(r"[\\/]", raw):
        return "chrome_profile must not contain '..'"
    on_windows = os.name == "nt"
    looks_windows_abs = bool(_WINDOWS_ABS_RE.match(raw)) or "\\" in raw
    if (looks_windows_abs and not on_windows) or (raw.startswith("/") and on_windows):
        return None  # foreign-OS absolute: ConfigService normalizes it into data/
    pure = PureWindowsPath(raw) if on_windows else PurePosixPath(raw)
    if not (pure.drive or pure.root):
        return None  # relative: anchored under the project root
    data_dir = _DATA_DIR.resolve()
    try:
        resolved = Path(raw).resolve()
    except (OSError, ValueError):
        return "chrome_profile is not a valid path"
    if resolved == data_dir or not resolved.is_relative_to(data_dir):
        return f"chrome_profile must be a directory inside {data_dir}"
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Sources
# ══════════════════════════════════════════════════════════════════════════════

class SourcesConfig(BaseModel):
    """Toggle which job boards the agent harvests from."""
    linkedin: bool = True
    naukri:   bool = False
    dice:     bool = False
    # Which saved LinkedIn account (session/profile) the harvest runs under.
    # "1" is the original single-account identity (backward compatible); "2" is
    # a second account with its own session file + Chrome profile. Selected in
    # the Rule Engine UI. See app/services/session_manager.py (account keying).
    linkedin_account: str = "1"

    @field_validator("linkedin_account", mode="before")
    @classmethod
    def _known_linkedin_account(cls, v: object) -> str:
        """Accept only LINKEDIN_ACCOUNTS ("1"/"2"); None and the legacy default
        aliases ("", "default", "primary") become "1". Anything else raises (422 on
        PUT /harvest-config): the id is joined into a filesystem path."""
        acct = "" if v is None else str(v).strip()
        if acct in _DEFAULT_ACCOUNT_ALIASES:
            return "1"
        if acct not in LINKEDIN_ACCOUNTS:
            raise ValueError(f"linkedin_account must be one of {', '.join(LINKEDIN_ACCOUNTS)}")
        return acct


# ══════════════════════════════════════════════════════════════════════════════
# Company Verification
# ══════════════════════════════════════════════════════════════════════════════

class VerificationConfig(BaseModel):
    """
    Company career-page verification settings.

    enabled        — run verification step after scraping
    method         — how to verify ("career_page" = visit company careers URL)
    on_mismatch    — action when career page found but job title not matched
    on_not_found   — action when career page cannot be reached
    """
    enabled:      bool                                  = False
    method:       Literal["career_page"]                = "career_page"
    on_mismatch:  Literal["skip", "flag", "include"]    = "flag"
    on_not_found: Literal["skip", "flag", "include"]    = "flag"


# ══════════════════════════════════════════════════════════════════════════════
# Filters (source of truth for ALL search + post-processing parameters)
# ══════════════════════════════════════════════════════════════════════════════

class FiltersConfig(BaseModel):
    """
    All search parameters and business-filter rules forwarded to every
    enabled source agent and the post-scraping filter pipeline.

    Values are set exclusively via the Rule Engine UI and persisted in
    harvest_config.json.  The backend never hardcodes any of these.
    """

    # ── Core search ───────────────────────────────────────────────────────────
    keyword:             str                                                                          = ""
    location:            str                                                                          = ""
    job_type:            Literal["Contract", "Permanent", "Part-time", "Freelance", "Full-time", "Any"] = "Any"
    work_mode:           Literal["Remote", "Hybrid", "Onsite", "Any"]                                = "Any"
    search_window_hours: Literal[24, 48, 72, 168, 720]                                               = 24
    # Safety cap — 0 means paginate until natural end of results.
    max_jobs:            int                                                                          = Field(500, ge=0, le=5000)

    # ── Domain ────────────────────────────────────────────────────────────────
    domain: Literal[
        "Data Engineering", "Data Science", "AI/ML", "SAP", "Cloud",
        "Digital", "UX/UI", "ERP", "Cyber Security", "Infrastructure",
        "IT", "Engineering", "Finance", "Operations", "Non-IT", "Any"
    ] = "Any"
    # Granular sub-categories selected under the IT/Non-IT Rule Engine UI, and
    # free-text custom titles entered under "Others". When a SUBSET of categories
    # is chosen these refine the LinkedIn `keywords=` search (OR-joined) — see
    # LinkedInAgent._compose_keyword_query; an empty list means "all selected",
    # which falls back to the coarse `domain` label above. `domain` still drives
    # post-scrape classification (domain_matches/infer_domain) either way.
    job_categories:    list[str] = Field(default_factory=list)
    custom_job_titles: list[str] = Field(default_factory=list)

    # Company-size pills selected in the Rule Engine UI (band tokens, e.g.
    # "501-1000"; "10001+"). Used ONLY as a minimum-size floor for the automated
    # email outreach gate (the lowest selected band's lower bound) — see
    # app.core.company_size.minimum_floor and auto_outreach_service. Never affects
    # scraping, search, storage, or the post-scrape flag pipeline: under-floor jobs
    # are still scraped and stored, they are only not emailed. Empty ⇒ no gate.
    company_sizes:     list[str] = Field(default_factory=list)

    # Naukri "Company type" filter (NAUKRI_COMPANY_TYPES labels, e.g. "Indian MNC").
    # Applied at search time as Naukri's own qbusinessSize URL param, so only
    # Naukri honours it — LinkedIn/Dice ignore it. Empty ⇒ all company types.
    naukri_company_types: list[str] = Field(default_factory=list)

    # ── Hiring entity + GCC ───────────────────────────────────────────────────
    hiring_entity: Literal["Direct Client", "GCC", "Ambiguous", "Staffing Firm", "Any"]             = "Any"
    gcc_mode:      Literal["include_gcc", "gcc_only", "exclude_gcc"]                                 = "include_gcc"

    # ── Salary filters ────────────────────────────────────────────────────────
    salary_min:                 int | None = None   # annual value in salary_currency
    salary_max:                 int | None = None
    salary_currency:            str        = "INR"  # ISO 4217
    include_undisclosed_salary: bool       = True   # keep jobs where salary is not disclosed

    # ── Company verification ──────────────────────────────────────────────────
    verification: VerificationConfig = Field(default_factory=VerificationConfig)

    @field_validator("naukri_company_types", mode="before")
    @classmethod
    def _known_company_types(cls, v: object) -> list[str]:
        """Keep only known labels (deduped, canonical order). Drops unknown values
        instead of raising — ConfigService.load swallows validation errors and
        would otherwise reset ALL settings to defaults on an already-saved file."""
        if not isinstance(v, (list, tuple)):
            return []
        wanted = {str(x).strip() for x in v}
        return [t for t in NAUKRI_COMPANY_TYPES if t in wanted]

    @model_validator(mode="after")
    def _reconcile_gcc(self) -> "FiltersConfig":
        """Auto-normalize the impossible gcc_mode × hiring_entity combinations.

        hiring_entity, when specific, already determines GCC-ness ("GCC" ⟺ is a
        GCC; Direct Client / Staffing Firm / Ambiguous ⟺ not a GCC), so gcc_mode
        is only meaningful when hiring_entity == "Any". A contradictory pair
        (e.g. gcc_only + Direct Client) would flag every job. Resolve by relaxing
        the less-specific gcc_mode → include_gcc, keeping the hiring_entity
        intent. Never raises — ConfigService.load swallows validation errors and
        would otherwise reset ALL settings to defaults on an already-saved file.
        """
        conflict = (
            (self.gcc_mode == "gcc_only" and self.hiring_entity in _NON_GCC_ENTITIES)
            or (self.gcc_mode == "exclude_gcc" and self.hiring_entity == "GCC")
        )
        if conflict:
            logger.warning(
                "filter_config_gcc_normalized",
                gcc_mode=self.gcc_mode, hiring_entity=self.hiring_entity,
                normalized_gcc_mode="include_gcc",
            )
            self.gcc_mode = "include_gcc"
        return self


# ══════════════════════════════════════════════════════════════════════════════
# Schedule
# ══════════════════════════════════════════════════════════════════════════════

class ScheduleConfig(BaseModel):
    """APScheduler trigger definition.

    tenant_id is the tenant a scheduled run (and its jobs) is stamped with; None
    keeps the pre-existing behaviour of running as the internal tenant."""
    frequency: Literal["hourly", "daily", "weekly"] = "daily"
    run_time:  str  = "09:00"          # HH:MM  (used when frequency == "daily" or "weekly")
    timezone:  str  = "Asia/Kolkata"   # IANA timezone name
    enabled:   bool = False
    tenant_id: str | None = None       # target tenant of scheduled runs (None = internal)

    @field_validator("tenant_id", mode="before")
    @classmethod
    def _known_tenant(cls, v: object) -> str | None:
        """None/blank → None (internal). Otherwise must be a seeded tenant id, else
        raises (422 on PUT /harvest-config)."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        from app.models.tenant import SEED_TENANTS

        known = [t["id"] for t in SEED_TENANTS]
        tid = str(v).strip()
        if tid not in known:
            raise ValueError(f"schedule.tenant_id must be one of {', '.join(known)}")
        return tid

    @field_validator("run_time", mode="before")
    @classmethod
    def _zero_pad_run_time(cls, v: object) -> object:
        """Zero-pad a hand-edited "7:00" → "07:00": the UI's <input type="time">
        renders a non-padded value as blank. Anything unparseable is left as-is
        (the scheduler falls back to 09:00) — never raises, since ConfigService.load
        would otherwise reset ALL settings to defaults."""
        if isinstance(v, str):
            parts = v.strip().split(":")
            if len(parts) == 2 and all(p.isdigit() for p in parts):
                h, m = int(parts[0]), int(parts[1])
                if 0 <= h <= 23 and 0 <= m <= 59:
                    return f"{h:02d}:{m:02d}"
        return v


# ══════════════════════════════════════════════════════════════════════════════
# Notifications (post-harvest report email)
# ══════════════════════════════════════════════════════════════════════════════

class NotificationConfig(BaseModel):
    """
    Post-harvest report email settings — reuses the SMTP transport already
    configured for OTP delivery (see Settings.smtp_*). Fully dynamic: toggle,
    recipients, attachment format, and subject line are all editable via
    PUT /harvest-config without a code change.

    subject_template supports {run_id}, {status}, {total_jobs} placeholders.
    email_cover_label is the display name of the report sender (the From address
    is Settings.otp_from_email). report_format=None sends the email with no
    attachments (summary only).
    """
    enabled:           bool                                   = False
    recipients:        list[str]                              = Field(default_factory=list)
    email_cover_label: str                                    = "SS Harvest Notification"
    report_format:     Literal["excel", "json", "both"] | None = "both"
    subject_template:  str = "Harvest Report — {run_id} ({status}, {total_jobs} jobs)"


# ══════════════════════════════════════════════════════════════════════════════
# Browser
# ══════════════════════════════════════════════════════════════════════════════

class BrowserConfig(BaseModel):
    """Playwright launch settings."""
    headless:       bool = False
    slow_mo_ms:     int  = Field(0, ge=0, le=3000)
    chrome_profile: str  = "data/chrome_profile"

    @field_validator("chrome_profile")
    @classmethod
    def _safe_chrome_profile(cls, v: str) -> str:
        """Reject a ".." path or an absolute one outside the data dir (422 on PUT
        /harvest-config); see chrome_profile_problem."""
        problem = chrome_profile_problem(v)
        if problem:
            raise ValueError(problem)
        return v

    @property
    def resolved_headless(self) -> bool:
        """
        Effective headless value for this run.

        SHOW_BROWSER, when set in the environment, overrides the JSON config
        so ops can force visible-browser mode (e.g. under Xvfb/x11vnc on a
        headless EC2 host) without editing harvest_config.json.
        """
        show_browser = os.getenv("SHOW_BROWSER")
        if show_browser is not None:
            return show_browser.strip().lower() not in ("1", "true", "yes", "on")
        return self.headless


# ══════════════════════════════════════════════════════════════════════════════
# Root config object
# ══════════════════════════════════════════════════════════════════════════════

class HarvestConfig(BaseModel):
    """Full harvest agent configuration — the root object in harvest_config.json."""
    sources:       SourcesConfig      = Field(default_factory=SourcesConfig)
    filters:       FiltersConfig      = Field(default_factory=FiltersConfig)
    schedule:      ScheduleConfig     = Field(default_factory=ScheduleConfig)
    browser:       BrowserConfig      = Field(default_factory=BrowserConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
