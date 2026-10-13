"""
Naukri.com Harvest Agent — guest job harvester (no login, no LLM).

Flow (mirrors the modern LinkedIn pipeline: _run → paginate → per-job detail →
incremental flush → post-harvest company / contact passes)
──────────────────────────────────────────────────────────────────────────────
1. Build the Naukri search-results (SRP) URL: an SEO path that encodes keyword +
   location (``python-developer-jobs-in-bengaluru[-N]``) plus EVERY applicable
   query filter (``k``, ``l`` on page 1, ``jobAge``, ``wfhType``, ``ctcFilter``).
2. Navigate normally. The SRP itself fetches ``GET /jobapi/v3/search`` (JSON);
   that response is captured with ``page.on("response")`` — never called
   directly, because it needs Naukri's own headers. Its request URL is used to
   VERIFY the filters actually reached Naukri's backend (pageNo / jobAge /
   wfhType / ctcFilter / k). Fallback: parse the DOM cards in ONE page.evaluate.
3. Skip jobs already seen this run (jobId) or already harvested in earlier runs
   (normalized URL / jobId) BEFORE any detail visit — skips don't use max_jobs.
4. Detail pass per job, opened like a click-through (a new tab of the same
   context, Referer = the results page): the job page fetches
   ``GET /jobapi/v4/job/{jobId}`` (captured the same way) → full description
   HTML, employment type, wfhType, industry, company website, consultant flag.
   Fallbacks: the page's ``application/ld+json`` JobPosting, then the
   ``[class*='dang-inner-html']`` description container. Visits are slow and
   adaptive (NaukriRateGovernor: 30–60 s gaps × a back-off multiplier, periodic
   breaks); a reCAPTCHA wall → cooldown + one retry, repeated walls → circuit
   breaker pause / half-open probe, optionally a human solving it in the Live
   Browser (wait_for_human). CAPTCHAs are never solved automatically, and a job
   whose detail can't be fetched is kept with its listing data.
5. Flush jobs to the caller every settings.harvest_persist_batch_size jobs.
6. Post-harvest (each pass isolated so none can lose jobs): shared company
   enrichment (Apollo, budget-capped) and the company+location HR-contact pass.

Filter URL parameters (verified live against naukri.com)
────────────────────────────────────────────────────────
work_mode  Remote → wfhType=2 | Hybrid → wfhType=3 | Onsite → wfhType=0
           (wfhType=1 is Naukri's legacy "temporary WFH (covid)" bucket)
job_type   NOT expressible — Naukri drops jobType from its search request; job
           type is judged post-scrape from each job's own employmentType.
date       24h → jobAge=1 | 48h → 2 | 72h → 3 | 7d → 7 | 30d → 30
salary     INR only, and only when undisclosed-salary jobs are NOT wanted →
           ctcFilter=<bucket> for every Naukri lakh bucket overlapping the range.
pagination ``…-jobs-N`` path suffix. Page N>1 must NOT carry ``l=`` (Naukri then
           redirects to page 1 and strips every param); location stays in the path.

Browser
───────
Guest only. Runs either as a tab in the caller's context (orchestrator "shared"
mode) or in its own cookie-less context (settings.naukri_browser_mode="isolated",
see naukri_guest_browser) on bundled Chromium (channel="chromium") — Naukri's
Akamai edge denies chromium-headless-shell.
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import math
import random
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import structlog
from playwright.async_api import Page

from app.config import get_settings
from app.core.contact_normalize import (
    extract_emails,
    html_contact_text,
    mailto_addresses,
    normalize_email,
    normalize_phone,
    tel_numbers,
)
from app.core.harvest_helpers import env_max_jobs_cap, retry_async
from app.core.job_url import normalize_job_url
from app.core.search_keywords import comma_joiner, compose_keyword_query
from app.core.text_formatting import (
    description_text_to_html,
    format_job_description,
    html_description_to_text,
    restructure_description_html,
    sanitize_description_html,
)
from app.models.harvest_models import FiltersConfig
from app.scrapers.browser_manager import BrowserManager, PersistentBrowserManager
from app.scrapers.human import human_scroll
from app.services import run_guard
from app.services.jd_llm_enrichment import (
    GroundedContacts,
    choose_email,
    has_contact_signals,
    is_flat_description,
    llm_configured,
    llm_extract_contacts,
    naukri_llm_settings,
    llm_restructure_description,
)

logger = structlog.get_logger(__name__)

StatusCallback = Callable[[str], Awaitable[None]]
BatchCallback = Callable[[list["NaukriScrapedJob"]], Awaitable[None]]
# Live-progress payload for the Rule Engine "Live Harvest" card — a dict of
# source_status.PROGRESS_KEYS (+ optional "message"); see SourceStatusTracker.progress.
ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]

_DEBUG_DIR         = Path("data/debug/naukri")
_DEBUG_SCREENSHOT  = Path("data/debug/screenshots")
_DEBUG_HTML        = Path("data/debug/html")

_NAUKRI_BASE = "https://www.naukri.com"
_PAGE_SIZE   = 20          # Naukri SRP page size
_MAX_PAGES   = 250         # hard safety stop for pagination
_IST         = timezone(timedelta(hours=5, minutes=30))

# Testing-only per-run cap (mirrors LINKEDIN_TEST_MAX_JOBS).
_TEST_MAX_JOBS_ENV = "NAUKRI_TEST_MAX_JOBS"


# ── Filter → URL param maps ───────────────────────────────────────────────────

# Naukri's own wfhType ids (from the SRP's wfhType cluster): 0 = Work from
# office, 2 = Remote, 3 = Hybrid; 1 = legacy "temporary WFH due to covid".
_WORK_MODE_MAP: dict[str, str] = {
    "Remote": "2",
    "Hybrid": "3",
    "Onsite": "0",
    "Any":    "",
}
# Per-job wfhType (v4 detail JSON) → our work_mode vocabulary.
_WFH_TYPE_TO_WORK_MODE: dict[str, str] = {
    "0": "onsite",
    "2": "remote",
    "3": "hybrid",
}
# FiltersConfig.search_window_hours (24/48/72/168/720) → Naukri jobAge days.
# Naukri accepts any day count (jobAge=2 verified live), not just its UI buckets.
_DATE_MAP: dict[int, str] = {
    24:  "1",
    48:  "2",
    72:  "3",
    168: "7",
    720: "30",
}
# Naukri salary buckets (lakhs per annum) as used by ctcFilter.
_CTC_BUCKETS: tuple[tuple[int, int], ...] = (
    (0, 3), (3, 6), (6, 10), (10, 15), (15, 25), (25, 50), (50, 75), (75, 100), (100, 500),
)

_JUNK_EMAIL_PATTERNS = ("reportfraud", "noreply", "no-reply", "donotreply", "fraud@", "abuse@",
                        "@naukri.com", "@infoedge")


class SearchFilterNotAppliedException(RuntimeError):
    """
    Raised when the requested search filters (jobAge / wfhType / ctcFilter /
    keyword) are not present in Naukri's own search request after navigation
    and one retry. Harvesting is aborted rather than collecting unfiltered
    (e.g. all-time) results.
    """


# ── Scraped job dataclass ─────────────────────────────────────────────────────

@dataclass
class NaukriScrapedJob:
    job_title:       str
    company:         str
    location:        str
    salary:          str
    experience:      str
    posted_date:     str
    job_url:         str
    job_description: str
    job_description_html: str  = ""   # sanitized rich HTML of the full description
    skills:          list[str] = field(default_factory=list)
    work_mode:       str       = "not_specified"
    source:          str       = "Naukri"
    job_id:          str       = ""   # Naukri jobId — the posting's natural key
    company_url:     str       = ""   # company's OWN website (v4 companyDetail.websiteUrl)
    company_size:    str       = ""   # Naukri doesn't publish one; filled by Apollo cache
    employment_type: str       = ""   # e.g. "Full Time, Permanent"
    industry_hint:   str       = ""   # Naukri industry, e.g. "IT Services & Consulting"
    # Hiring signals from Naukri's own data: consultant=True ⇒ posted by a
    # staffing/recruitment consultant; hiring_for = the client named on it.
    # None ⇒ Naukri didn't say.
    consultant:      bool | None = None
    hiring_for:      str       = ""
    # Lead intelligence — only ever values present verbatim in Naukri's data.
    recruiter_name:         str | None = None
    recruiter_company:      str | None = None
    job_poster_designation: str | None = None
    email_id:               str | None = None
    contact_number:         str | None = None
    linkedin_profile_url:   str | None = None
    # Internal (not persisted): the raw description HTML the text was built
    # from — contact extraction / LLM grounding read it (mailto: links, tag
    # boundaries) — and where each contact field came from: "naukri" (Naukri's
    # own recruiter block), "jd_text" (regex over the description) or "jd_llm"
    # (grounded LLM extraction). Only "jd_text" values may be replaced by the LLM.
    description_source_html: str = field(default="", repr=False, compare=False)
    contact_sources: dict[str, str] = field(default_factory=dict, repr=False, compare=False)

    # LinkedIn-compatible aliases — shared post-harvest passes (e.g.
    # company_location_contact_service.discover_company_location_contacts) read
    # job_poster_* via getattr.
    @property
    def job_poster_name(self) -> str | None:
        return self.recruiter_name

    @property
    def job_poster_company(self) -> str | None:
        return self.recruiter_company

    @property
    def job_poster_email(self) -> str | None:
        return self.email_id

    @property
    def job_poster_phone(self) -> str | None:
        return self.contact_number


# ── Overlay selectors ─────────────────────────────────────────────────────────

_COOKIE_SELECTORS: tuple[str, ...] = (
    "button[class*='accept']",
    "button:has-text('Accept')",
    "button:has-text('Accept All')",
    "button:has-text('OK')",
)
_MODAL_DISMISS_SELECTORS: tuple[str, ...] = (
    "button[class*='close']",
    "span[class*='close-icon']",
    "button[aria-label='Close']",
    "button:has-text('Skip')",
    "button:has-text('Not now')",
    "button:has-text('Cancel')",
    ".modal-close",
)

# All SRP cards parsed in ONE round-trip (fallback when the v3 JSON wasn't captured).
_DOM_CARDS_JS = r"""
() => {
  let cards = Array.from(document.querySelectorAll('div.srp-jobtuple-wrapper[data-job-id]'));
  if (!cards.length) cards = Array.from(document.querySelectorAll('[data-job-id]'));
  const out = [];
  for (const card of cards) {
    try {
      const pick = (sels) => {
        for (const s of sels) {
          const e = card.querySelector(s);
          if (!e) continue;
          const v = (e.getAttribute('title') || e.textContent || '').trim();
          if (v) return v;
        }
        return '';
      };
      const a = card.querySelector('a.title') || card.querySelector('a[href*="job-listings"]');
      out.push({
        job_id:      card.getAttribute('data-job-id') || '',
        title:       a ? (a.getAttribute('title') || a.textContent || '').trim() : '',
        url:         a ? (a.href || '') : '',
        company:     pick(['a.comp-name', '[class*="comp-name"]']),
        location:    pick(['span.locWdth', '[class*="loc"] [title]', '[class*="loc"]']),
        experience:  pick(['span.expwdth', '[class*="exp"] [title]', '[class*="exp"]']),
        salary:      pick(['[class*="sal"] [title]', '[class*="sal"]']),
        description: pick(['span.job-desc', '[class*="job-desc"]']),
        skills:      Array.from(card.querySelectorAll('ul.tags-gt li, ul[class*="tag"] li'))
                       .map(li => (li.textContent || '').trim()).filter(Boolean),
        posted:      pick(['span.job-post-day', '[class*="job-post-day"]']),
      });
    } catch (e) { /* one bad card never breaks the page */ }
  }
  return out;
}
"""

# Detail-page fallbacks (when the v4 JSON wasn't captured), in ONE round-trip.
_DETAIL_FALLBACK_JS = r"""
() => {
  const ld = Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
               .map(s => s.textContent || '');
  const d = document.querySelector('[class*="job-desc-container"] [class*="dang-inner-html"]')
         || document.querySelector('[class*="dang-inner-html"]');
  return {ld: ld, desc_html: d ? d.innerHTML : '', title: document.title || ''};
}
"""


# ── Pure helpers (unit-tested) ────────────────────────────────────────────────

_UNICODE_JUNK = re.compile(r"[ ​‌‍﻿­]")
_WHITESPACE   = re.compile(r"[ \t]{2,}")


def _clean(text: Any) -> str:
    text = _UNICODE_JUNK.sub(" ", str(text or ""))
    text = _WHITESPACE.sub(" ", text)
    return re.sub(r"\n+", " ", text).strip()


def _infer_work_mode(text: str) -> str:
    t = (text or "").lower()
    if "remote" in t or "work from home" in t or "wfh" in t:
        return "remote"
    if "hybrid" in t:
        return "hybrid"
    if "on-site" in t or "onsite" in t or "work from office" in t:
        return "onsite"
    return "not_specified"


def _work_mode_from_wfh_type(code: Any) -> str:
    """Naukri per-job wfhType id → work_mode ("" when unknown/absent)."""
    return _WFH_TYPE_TO_WORK_MODE.get(str(code).strip(), "") if code is not None else ""


def _epoch_ms_to_date(value: Any) -> str:
    """Naukri createdDate (epoch milliseconds) → UTC YYYY-MM-DD, or ""."""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return ""
    if ms <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


def _ist_datetime_to_date(value: Any) -> str:
    """v4 createdDate ("2026-10-07 18:49:46", India time) / ISO date → UTC
    YYYY-MM-DD, or ""."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.isdigit():
        return _epoch_ms_to_date(raw)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw                      # a bare calendar date has no timezone to shift
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        m = re.match(r"\d{4}-\d{2}-\d{2}", raw)
        return m.group(0) if m else ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_IST)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


_REL_NUM_RE = re.compile(r"(\d+)\s*\+?\s*(minute|min|hour|hr|day|week|month)s?", re.I)


def _resolve_relative_posted(raw: str, ref: datetime) -> str:
    """Naukri card freshness label → YYYY-MM-DD relative to `ref` (the run's
    pinned start). Handles "Just now", "Few hours ago", "Today", "Yesterday",
    "N days ago", "30+ Days Ago", "N weeks/months ago", and absolute ISO dates.
    Returns "" when the label can't be resolved (never guesses "today")."""
    text = (raw or "").strip().lower()
    if not text:
        return ""
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if m:
        return m.group(0)
    if any(p in text for p in ("just now", "few hours", "hour ago", "hours ago",
                               "minute", "today", "few mins", "moments ago")):
        return ref.strftime("%Y-%m-%d")
    if "yesterday" in text:
        return (ref - timedelta(days=1)).strftime("%Y-%m-%d")
    m = _REL_NUM_RE.search(text)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        if unit in ("minute", "min", "hour", "hr"):
            return ref.strftime("%Y-%m-%d")
        days = n * {"day": 1, "week": 7, "month": 30}[unit]
        return (ref - timedelta(days=days)).strftime("%Y-%m-%d")
    return ""


def _abs_url(href: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return _NAUKRI_BASE + href
    return href


_JOB_ID_FROM_URL_RE = re.compile(r"-(\d{9,15})$")


def naukri_job_id_from_url(url: str | None) -> str:
    """Trailing Naukri jobId of a naukri.com job-listing URL ("" when absent or
    when the URL belongs to another board)."""
    norm = normalize_job_url(url)
    if "naukri.com" not in norm:
        return ""
    m = _JOB_ID_FROM_URL_RE.search(norm)
    return m.group(1) if m else ""


def _slug(text: str) -> str:
    """Naukri SEO slug: lowercase, c++ → c-plus-plus, c# → c-sharp, .net → dot-net,
    every other non-alphanumeric run → '-'."""
    t = (text or "").lower()
    t = t.replace("++", " plus plus ").replace("#", " sharp ")
    t = re.sub(r"(^|\s)\.net\b", r"\1 dot net ", t)
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def _salary_buckets(f: FiltersConfig) -> list[str]:
    """Naukri ctcFilter buckets overlapping [salary_min, salary_max] (INR/yr), or
    [] when no INR salary range is set OR undisclosed-salary jobs are wanted
    (Naukri's salary filter would drop those — the post-scrape salary check in
    BusinessFilterService handles the range instead)."""
    if (f.salary_currency or "").upper() != "INR" or f.include_undisclosed_salary:
        return []
    if not f.salary_min and not f.salary_max:
        return []
    lo = (f.salary_min or 0) / 100_000
    hi = (f.salary_max / 100_000) if f.salary_max else math.inf
    return [f"{a}to{b}" for a, b in _CTC_BUCKETS if a < hi and b > lo]


def _keyword_for(f: FiltersConfig) -> str:
    """Naukri `k=` value: typed keyword, else comma-joined custom titles /
    category subset, else the coarse domain (shared composition rules)."""
    return compose_keyword_query(
        f.keyword, f.domain, f.job_categories, f.custom_job_titles, joiner=comma_joiner,
    )


def _expected_filter_params(f: FiltersConfig) -> dict[str, list[str]]:
    """The filter params Naukri's own search request must carry for this run."""
    exp: dict[str, list[str]] = {}
    if ja := _DATE_MAP.get(f.search_window_hours, ""):
        exp["jobAge"] = [ja]
    if wt := _WORK_MODE_MAP.get(f.work_mode, ""):
        exp["wfhType"] = [wt]
    if ctc := _salary_buckets(f):
        exp["ctcFilter"] = ctc
    return exp


def build_search_url(f: FiltersConfig, page_no: int = 1) -> str:
    """Naukri SRP URL for `page_no` with every applicable filter.

    Path: ``{keyword}-jobs`` / ``{keyword}-jobs-in-{location}`` /
    ``jobs-in-{location}`` / ``jobs-in-india``, with ``-{N}`` for page N>1.
    Query: ``k`` (keyword, every page), ``l`` (location, page 1 ONLY — on later
    pages Naukri redirects an ``l=`` URL to page 1 and strips every param; the
    location is carried by the path), ``jobAge``, ``wfhType``, ``ctcFilter``.
    """
    keyword = _keyword_for(f)
    location = (f.location or "").strip()
    k_slug, l_slug = _slug(keyword), _slug(location)
    if k_slug and l_slug:
        path = f"{k_slug}-jobs-in-{l_slug}"
    elif k_slug:
        path = f"{k_slug}-jobs"
    elif l_slug:
        path = f"jobs-in-{l_slug}"
    else:
        path = "jobs-in-india"
    if page_no > 1:
        path = f"{path}-{page_no}"

    params: list[tuple[str, str]] = []
    if keyword:
        params.append(("k", keyword))
    if location and page_no == 1:
        params.append(("l", location))
    for key, values in _expected_filter_params(f).items():
        params.extend((key, v) for v in values)
    query = urlencode(params)
    return f"{_NAUKRI_BASE}/{path}" + (f"?{query}" if query else "")


def _norm_kw(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def missing_search_filters(search_url: str, f: FiltersConfig, page_no: int) -> list[str]:
    """Which requested filters are ABSENT from Naukri's own search request URL
    (the intercepted /jobapi/v3/search, or the SRP URL as a fallback). Returns
    [] when everything requested was applied."""
    try:
        q = parse_qs(urlparse(search_url).query, keep_blank_values=True)
    except ValueError:
        return ["unparseable_url"]
    missing: list[str] = []
    for key, values in _expected_filter_params(f).items():
        have = set(q.get(key, []))
        if not set(values) <= have:
            missing.append(key)
    if "/jobapi/" in search_url:
        page_vals = q.get("pageNo", [])
        if page_vals and page_vals[0] != str(page_no):
            missing.append("pageNo")
    keyword = _keyword_for(f)
    if keyword:
        sent = q.get("k", []) + q.get("keyword", [])
        if not any(_norm_kw(v) == _norm_kw(keyword) for v in sent):
            missing.append("k")
    return missing


def _placeholder(d: dict, kind: str) -> str:
    for ph in d.get("placeholders") or []:
        if isinstance(ph, dict) and ph.get("type") == kind:
            return _clean(ph.get("label", ""))
    return ""


def _split_skills(raw: Any) -> list[str]:
    if isinstance(raw, list):
        items = [_clean(x) for x in raw]
    else:
        items = [_clean(x) for x in str(raw or "").split(",")]
    out: list[str] = []
    seen: set[str] = set()
    for s in items:
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out[:15]


def _snippet_to_text(snippet: str) -> str:
    if not snippet:
        return ""
    if "<" in snippet:
        return html_description_to_text(snippet) or _clean(re.sub(r"<[^>]+>", " ", snippet))
    return format_job_description(snippet)


def _salary_label(raw: str) -> str:
    raw = _clean(raw)
    if not raw or raw.lower() in ("not disclosed", "not specified"):
        return "Not Disclosed"
    return raw


def job_from_search_json(d: dict, ref: datetime) -> NaukriScrapedJob | None:
    """One /jobapi/v3/search ``jobDetails[]`` entry → NaukriScrapedJob, or None
    when it has no usable title/URL."""
    if not isinstance(d, dict):
        return None
    title = _clean(d.get("title"))
    url = _abs_url(d.get("jdURL") or d.get("staticUrl") or "")
    if not title or not url or "job-listings" not in url:
        return None
    location = _placeholder(d, "location")
    posted = _epoch_ms_to_date(d.get("createdDate")) or _resolve_relative_posted(
        str(d.get("footerPlaceholderLabel") or ""), ref,
    )
    consultant = d.get("consultant") if isinstance(d.get("consultant"), bool) else None
    company = _clean(d.get("companyName"))
    return NaukriScrapedJob(
        job_title       = title,
        company         = company or "Unknown Company",
        location        = location or "Unknown Location",
        salary          = _salary_label(_placeholder(d, "salary")),
        experience      = _placeholder(d, "experience") or _clean(d.get("experienceText")) or "Not Specified",
        posted_date     = posted,
        job_url         = url,
        job_description = _snippet_to_text(str(d.get("jobDescription") or "")),
        skills          = _split_skills(d.get("tagsAndSkills")),
        work_mode       = _infer_work_mode(location),
        job_id          = str(d.get("jobId") or "") or naukri_job_id_from_url(url),
        consultant      = consultant,
        hiring_for      = _clean(d.get("hiringFor") or d.get("clientTitleString")),
    )


def job_from_dom_card(d: dict, ref: datetime) -> NaukriScrapedJob | None:
    """One DOM-card dict (see _DOM_CARDS_JS) → NaukriScrapedJob, or None."""
    if not isinstance(d, dict):
        return None
    title = _clean(d.get("title"))
    url = _abs_url(str(d.get("url") or "").split("?")[0])
    if not title or not url:
        return None
    location = _clean(d.get("location"))
    return NaukriScrapedJob(
        job_title       = title,
        company         = _clean(d.get("company")) or "Unknown Company",
        location        = location or "Unknown Location",
        salary          = _salary_label(d.get("salary") or ""),
        experience      = _clean(d.get("experience")) or "Not Specified",
        posted_date     = _resolve_relative_posted(str(d.get("posted") or ""), ref),
        job_url         = url,
        job_description = format_job_description(_clean(d.get("description"))),
        skills          = _split_skills(d.get("skills") or []),
        work_mode       = _infer_work_mode(location),
        job_id          = str(d.get("job_id") or "") or naukri_job_id_from_url(url),
    )


# Recruiter/contact blocks Naukri may attach to a v4 job when it chooses to show
# them (showRecruiterDetail). Read defensively — guests usually get none.
_RECRUITER_KEYS = ("recruiterDetail", "recruiterDetails", "recruiterProfile",
                   "contactDetails", "recruiter")

# Emails come from the shared extractor (app/core/contact_normalize.extract_emails):
# mailto: targets, then tag-boundary text of the raw HTML, then the formatted
# text — each match trimmed of text glued onto the domain
# ("x@infosys.comCTCIndustryTesting" → "x@infosys.com").
# A phone is only taken from free text when a contact cue sits right before it,
# so job ids / salary figures are never mistaken for numbers.
_PHONE_IN_TEXT_RE = re.compile(
    r"(?:call|contact|mobile|mob|phone|ph|whats\s*app|tel)\s*(?:no\.?|number|#)?\s*[:\-.]?\s*"
    r"(\+?\d[\d\s\-()]{7,16}\d)",
    re.I,
)


def _clean_email(raw: str | None) -> str | None:
    email = normalize_email(raw)
    if email and any(p in email.lower() for p in _JUNK_EMAIL_PATTERNS):
        return None
    return email


def extract_contacts_from_text(text: str, html: str = "") -> tuple[str | None, str | None]:
    """(email, phone) present VERBATIM in a job's description, or None each.

    ``text`` is the formatted description; ``html`` (optional) the raw source
    HTML, whose mailto:/tel: links and tag boundaries are read too, so text from
    adjacent elements can't glue onto an address. Junk/system addresses
    (report-fraud, no-reply, Naukri's own) are dropped; values go through the
    shared contact normalizers."""
    email = next((e for e in map(_clean_email, extract_emails(text, html)) if e), None)
    phone = None
    for raw in tel_numbers(html):
        if (phone := normalize_phone(raw)):
            break
    if phone is None:
        for source in (html_contact_text(html), text or ""):
            for m in _PHONE_IN_TEXT_RE.finditer(source):
                if (phone := normalize_phone(m.group(1))):
                    break
            if phone:
                break
    return email, phone


def _apply_contacts(job: NaukriScrapedJob, name: Any = None, designation: Any = None,
                    company: Any = None, email: Any = None, phone: Any = None,
                    source: str = "naukri") -> None:
    """Fill recruiter fields only where empty and only with real values;
    record where each filled value came from (job.contact_sources)."""
    if (v := _clean(name)) and not job.recruiter_name:
        job.recruiter_name = v
        job.contact_sources["name"] = source
    if (v := _clean(designation)) and not job.job_poster_designation:
        job.job_poster_designation = v
        job.contact_sources["designation"] = source
    if (v := _clean(company)) and not job.recruiter_company:
        job.recruiter_company = v
        job.contact_sources["company"] = source
    if email and not job.email_id and (e := _clean_email(str(email))):
        job.email_id = e
        job.contact_sources["email"] = source
    if phone and not job.contact_number and (p := normalize_phone(str(phone))):
        job.contact_number = p
        job.contact_sources["phone"] = source


def _apply_text_contacts(job: NaukriScrapedJob) -> None:
    """Deterministic contact pass over the job's description (text + raw HTML)."""
    email, phone = extract_contacts_from_text(job.job_description, job.description_source_html)
    _apply_contacts(job, email=email, phone=phone, source="jd_text")


def apply_llm_contacts(job: NaukriScrapedJob, grounded: GroundedContacts) -> list[str]:
    """Merge grounded LLM contacts into ``job``; returns the fields changed.

    Values from Naukri's own recruiter block are never touched. The email is
    reconciled with the deterministic one (choose_email): a shorter verbatim
    prefix of a glued regex match, or a personal recruiter address over a
    generic inbox, replaces a regex ("jd_text") value. Phone / name /
    designation / company only fill empty fields."""
    changed: list[str] = []
    if job.contact_sources.get("email") != "naukri":
        current = job.email_id if job.contact_sources.get("email") == "jd_text" else None
        if job.email_id is None or current is not None:
            chosen, origin = choose_email(current, grounded)
            if chosen and origin == "llm" and chosen != job.email_id:
                job.email_id = chosen
                job.contact_sources["email"] = "jd_llm"
                changed.append("email")
    for fld, attr, value in (
        ("phone", "contact_number", grounded.phones[0] if grounded.phones else None),
        ("name", "recruiter_name", grounded.name),
        ("designation", "job_poster_designation", grounded.designation),
        ("company", "recruiter_company", grounded.company),
    ):
        if value and not getattr(job, attr):
            setattr(job, attr, value)
            job.contact_sources[fld] = "jd_llm"
            changed.append(fld)
    return changed


def _set_description(job: NaukriScrapedJob, html: str) -> bool:
    """Set job_description (formatted text) + job_description_html from a
    description that may be HTML or plain text. Recruiter-authored HTML is
    first repaired (restructure_description_html: one bullet per line, headings
    split out, raw newlines kept) so both forms keep the list structure."""
    html = (html or "").strip()
    if not html:
        return False
    if "<" not in html and "&lt;" in html:
        html = _html.unescape(html)              # entity-encoded HTML (ld+json)
    if "<" in html:
        fixed = restructure_description_html(html)
        text = html_description_to_text(fixed)
        rich = sanitize_description_html(fixed)
    else:
        text = format_job_description(html)
        rich = ""                                # plain text: built from the text below
    if not text:
        return False
    job.job_description = text
    job.job_description_html = rich or description_text_to_html(text)
    job.description_source_html = html
    return True


_BARE_WORK_MODE_LOCATIONS = {"remote", "hybrid", "work from home", "wfh", "unknown location", ""}


def apply_detail_json(job: NaukriScrapedJob, payload: dict) -> bool:
    """Merge a /jobapi/v4/job/{id} payload into `job`. Returns True when it
    carried a usable jobDetails block."""
    jd = (payload or {}).get("jobDetails") if isinstance(payload, dict) else None
    if not isinstance(jd, dict):
        return False

    _set_description(job, str(jd.get("description") or ""))

    if emp := _clean(jd.get("employmentType")):
        job.employment_type = emp
    wm = _work_mode_from_wfh_type(jd.get("wfhType"))
    if wm:
        job.work_mode = wm
    elif job.work_mode == "not_specified":
        job.work_mode = _infer_work_mode(str(jd.get("wfhLabel") or ""))
    if ind := _clean(jd.get("industry")):
        job.industry_hint = ind
    if isinstance(jd.get("consultant"), bool):
        job.consultant = jd["consultant"]

    cd = jd.get("companyDetail") if isinstance(jd.get("companyDetail"), dict) else {}
    if site := _clean(cd.get("websiteUrl")):
        job.company_url = site
    if not job.hiring_for:
        job.hiring_for = _clean(cd.get("hiringFor") or jd.get("clientTitleString"))
    if job.company in ("", "Unknown Company") and (name := _clean(cd.get("name"))):
        job.company = name

    labels = [_clean(loc.get("label")) for loc in jd.get("locations") or [] if isinstance(loc, dict)]
    labels = [lbl for lbl in labels if lbl]
    if labels and job.location.strip().lower() in _BARE_WORK_MODE_LOCATIONS:
        job.location = ", ".join(labels)

    ks = jd.get("keySkills") if isinstance(jd.get("keySkills"), dict) else {}
    detail_skills = [
        s.get("label") for grp in ("preferred", "other") for s in (ks.get(grp) or [])
        if isinstance(s, dict)
    ]
    if detail_skills:
        job.skills = _split_skills(list(job.skills) + detail_skills)

    sal = jd.get("salaryDetail") if isinstance(jd.get("salaryDetail"), dict) else {}
    if job.salary == "Not Disclosed" and not sal.get("hideSalary") and (st := _clean(sal.get("salaryText") or sal.get("label"))):
        job.salary = st
    if not job.posted_date:
        job.posted_date = _ist_datetime_to_date(jd.get("createdDate"))
    if job.experience == "Not Specified" and (ex := _clean(jd.get("experienceText"))):
        job.experience = ex

    # Recruiter contact — only when Naukri itself exposes one.
    for key in _RECRUITER_KEYS:
        rec = jd.get(key)
        if isinstance(rec, dict):
            _apply_contacts(
                job,
                name=rec.get("name") or rec.get("recruiterName") or rec.get("contactName"),
                designation=rec.get("designation") or rec.get("title"),
                company=rec.get("companyName") or rec.get("company"),
                email=rec.get("email") or rec.get("emailId"),
                phone=rec.get("mobile") or rec.get("phone") or rec.get("contactNumber"),
                source="naukri",
            )
    _apply_text_contacts(job)
    return True


def _find_job_posting(obj: Any) -> dict | None:
    if isinstance(obj, list):
        for item in obj:
            if (found := _find_job_posting(item)) is not None:
                return found
        return None
    if isinstance(obj, dict):
        t = obj.get("@type")
        if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
            return obj
        if "@graph" in obj:
            return _find_job_posting(obj["@graph"])
    return None


def apply_ld_json(job: NaukriScrapedJob, ld_texts: list[str]) -> bool:
    """Merge the page's schema.org JobPosting (application/ld+json) into `job`.
    Returns True when a JobPosting with a description was found."""
    posting = None
    for raw in ld_texts or []:
        try:
            posting = _find_job_posting(json.loads(raw))
        except (ValueError, TypeError):
            continue
        if posting:
            break
    if not posting:
        return False
    if not _set_description(job, str(posting.get("description") or "")):
        return False

    emp = posting.get("employmentType")
    if isinstance(emp, list):
        emp = ", ".join(str(e) for e in emp if e)
    if emp and not job.employment_type:
        job.employment_type = _clean(str(emp).replace("_", " ").title())
    if str(posting.get("jobLocationType") or "").upper() == "TELECOMMUTE":
        job.work_mode = "remote"
    if (ind := _clean(posting.get("industry"))) and not job.industry_hint:
        job.industry_hint = ind
    org = posting.get("hiringOrganization") if isinstance(posting.get("hiringOrganization"), dict) else {}
    same_as = _clean(org.get("sameAs") or org.get("url"))
    if same_as and "naukri.com" not in same_as.lower() and not job.company_url:
        job.company_url = same_as
    if job.company in ("", "Unknown Company") and (name := _clean(org.get("name"))):
        job.company = name
    if not job.posted_date:
        job.posted_date = _ist_datetime_to_date(posting.get("datePosted"))
    if (skills := posting.get("skills")) and not job.skills:
        job.skills = _split_skills(skills)
    _apply_text_contacts(job)
    return True


def apply_dom_description(job: NaukriScrapedJob, desc_html: str) -> bool:
    """Last-resort detail fallback: the rendered description container's HTML."""
    if not _set_description(job, desc_html):
        return False
    if job.work_mode == "not_specified":
        job.work_mode = _infer_work_mode(job.location)
    _apply_text_contacts(job)
    return True


# ── Browser factory for isolated guest mode ───────────────────────────────────

def naukri_guest_browser(headless: bool = True, slow_mo: int = 0) -> BrowserManager:
    """A fresh cookie-less guest browser for Naukri, independent of any LinkedIn
    Chrome profile. Bundled Chromium via channel="chromium" (NOT the
    chromium-headless-shell, which Naukri's Akamai edge answers with "Access
    Denied"); Indian locale/timezone like a real visitor. Cross-platform."""
    return BrowserManager(
        headless=headless, slow_mo=slow_mo,
        channel="chromium", locale="en-IN", timezone_id="Asia/Kolkata",
    )


async def _delay(page: Page, lo: int, hi: int) -> None:
    await page.wait_for_timeout(random.randint(lo, hi))


# ── Rate governor / circuit breaker (detail-page pacing) ─────────────────────
#
# www.naukri.com answers a too-fast stream of job-detail visits with a full-page
# reCAPTCHA wall (a request-rate threshold: clean pages in between, a wall every
# few minutes). Nothing here solves CAPTCHAs — the run slows down, cools off,
# and (when the caller allows it) asks a human to tick the box in the Live
# Browser. Every wait is cooperative: ≤ _WAIT_CHUNK_S slices that end early on a
# user stop.

_WAIT_CHUNK_S       = 2.0        # max single sleep inside a cooperative wait
_HUMAN_POLL_S       = 3.0        # how often a human-held CAPTCHA tab is re-checked
_HUMAN_WAIT_SPACING = 30 * 60    # at most one human wait per run per 30 min


def _wall_time() -> float:
    """Epoch seconds for progress ``resume_at`` (module-level so tests can pin it)."""
    return time.time()


def _monotonic() -> float:
    """Monotonic clock for the human-wait spacing (patchable in tests)."""
    return time.monotonic()


async def _sleep_chunk(page: Any | None, seconds: float) -> None:
    """One slice of a cooperative wait: the page clock when there is a live
    page (keeps Playwright's loop serviced), asyncio.sleep otherwise / when the
    page is gone. Module-level so tests can patch it out."""
    if seconds <= 0:
        return
    if page is not None:
        try:
            await page.wait_for_timeout(int(seconds * 1000))
            return
        except Exception:
            pass
    await asyncio.sleep(seconds)


class NaukriRateGovernor:
    """Adaptive gap between detail visits.

    gap        uniform[min_ms, max(min_ms, max_ms)] × multiplier
    on_block   multiplier doubles (capped at max_multiplier)
    on_clean   after ``clean_streak`` consecutive clean pages: one step back
               (multiplier ÷ 2, floor 1.0)
    breaks     every ``break_every`` detail visits (0 = off) a longer break of
               uniform[break_min_s, break_max_s]
    """

    def __init__(
        self,
        min_ms: int,
        max_ms: int,
        max_multiplier: float = 4.0,
        clean_streak: int = 10,
        break_every: int = 0,
        break_min_s: float = 0.0,
        break_max_s: float = 0.0,
        rng: random.Random | None = None,
    ) -> None:
        self.min_ms = max(0, int(min_ms))
        self.max_ms = max(self.min_ms, int(max_ms))
        self.max_multiplier = max(1.0, float(max_multiplier))
        self.clean_streak = max(1, int(clean_streak))
        self.break_every = max(0, int(break_every))
        self.break_min_s = max(0.0, float(break_min_s))
        self.break_max_s = max(self.break_min_s, float(break_max_s))
        self._rng = rng or random.Random()
        self.multiplier = 1.0
        self.clean = 0            # consecutive clean detail pages
        self.visits = 0           # detail page loads this run (incl. retries)
        self.since_break = 0      # detail page loads since the last rest

    @classmethod
    def from_settings(cls, settings: Any) -> "NaukriRateGovernor":
        return cls(
            min_ms         = settings.naukri_detail_delay_min_ms,
            max_ms         = settings.naukri_detail_delay_max_ms,
            max_multiplier = settings.naukri_backoff_max_multiplier,
            clean_streak   = settings.naukri_backoff_clean_streak,
            break_every    = settings.naukri_break_every_details,
            break_min_s    = settings.naukri_break_min_s,
            break_max_s    = settings.naukri_break_max_s,
        )

    def next_gap_s(self) -> float:
        return self._rng.uniform(self.min_ms, self.max_ms) / 1000.0 * self.multiplier

    def note_visit(self) -> None:
        self.visits += 1
        self.since_break += 1

    def on_clean(self) -> None:
        self.clean += 1
        if self.clean >= self.clean_streak:
            self.clean = 0
            if self.multiplier > 1.0:
                self.multiplier = max(1.0, self.multiplier / 2)

    def on_block(self) -> None:
        self.clean = 0
        self.multiplier = min(self.multiplier * 2, self.max_multiplier)

    def break_due(self) -> bool:
        return self.break_every > 0 and self.since_break >= self.break_every

    def break_s(self) -> float:
        return self._rng.uniform(self.break_min_s, self.break_max_s)

    def rested(self) -> None:
        """A break / cooldown / breaker pause just happened — restart the break count."""
        self.since_break = 0


class NaukriCircuitBreaker:
    """CLOSED → (``threshold`` consecutive blocks) → OPEN → pause → HALF_OPEN →
    one probe: success → CLOSED, block → OPEN again with the pause doubled
    (capped at 2 × pause_max_s)."""

    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(
        self,
        threshold: int,
        pause_min_s: float,
        pause_max_s: float,
        rng: random.Random | None = None,
    ) -> None:
        self.threshold = max(1, int(threshold))
        self.pause_min_s = max(0.0, float(pause_min_s))
        self.pause_max_s = max(self.pause_min_s, float(pause_max_s))
        self._rng = rng or random.Random()
        self.state = self.CLOSED
        self.consecutive = 0      # consecutive blocked detail loads (retries count)
        self.reopens = 0          # HALF_OPEN probes that failed since the last CLOSED
        self.trips = 0            # transitions into OPEN this run

    @classmethod
    def from_settings(cls, settings: Any) -> "NaukriCircuitBreaker":
        return cls(
            threshold   = settings.naukri_breaker_threshold,
            pause_min_s = settings.naukri_breaker_pause_min_s,
            pause_max_s = settings.naukri_breaker_pause_max_s,
        )

    def would_trip(self) -> bool:
        """Whether one more block would open the breaker."""
        return self.state == self.HALF_OPEN or self.consecutive + 1 >= self.threshold

    def on_block(self) -> bool:
        """Record a blocked load. True when this block (newly) opened the breaker."""
        self.consecutive += 1
        if self.state == self.HALF_OPEN:
            self.state = self.OPEN
            self.reopens += 1
            self.trips += 1
            return True
        if self.state == self.CLOSED and self.consecutive >= self.threshold:
            self.state = self.OPEN
            self.trips += 1
            return True
        return False

    def on_success(self) -> None:
        self.state = self.CLOSED
        self.consecutive = 0
        self.reopens = 0

    def half_open(self) -> None:
        if self.state == self.OPEN:
            self.state = self.HALF_OPEN

    def pause_s(self) -> float:
        base = self._rng.uniform(self.pause_min_s, self.pause_max_s) * (2 ** self.reopens)
        return min(base, 2 * self.pause_max_s)


@dataclass
class NaukriBlockStats:
    """Per-run block / recovery counters (the ``naukri_block_summary`` log)."""
    detail_blocks: int = 0
    srp_blocks: int = 0
    retries_ok: int = 0
    retries_failed: int = 0
    breaker_trips: int = 0
    breaks: int = 0
    human_waits: int = 0
    human_solved: int = 0
    cooldown_seconds_total: float = 0.0
    final_multiplier: float = 1.0

    def as_log(self) -> dict[str, Any]:
        out = asdict(self)
        out["cooldown_seconds_total"] = int(round(self.cooldown_seconds_total))
        return out


# ══════════════════════════════════════════════════════════════════════════════
# Naukri Agent
# ══════════════════════════════════════════════════════════════════════════════

class NaukriAgent:
    """
    Naukri.com guest job harvester. Instantiate fresh for each run.

    already_harvested  normalized job_urls harvested within the lookback window
                       (loaded by the route); matching postings — by URL or by
                       their trailing Naukri jobId — are skipped BEFORE the
                       detail visit and don't consume max_jobs.
    """

    # Consecutive LLM failures (errors / timeouts) after which LLM enrichment is
    # switched off for the rest of the run — deterministic results stand.
    _LLM_MAX_FAILURE_STREAK = 3

    def __init__(
        self,
        already_harvested: set[str] | None = None,
        llm_service: Any | None = None,
        wait_for_human: bool = False,
    ) -> None:
        # When the circuit breaker trips, hold the CAPTCHA tab open in the Live
        # Browser and wait (bounded, settings.naukri_human_wait_s) for a person
        # to tick the box before falling back to the timed pause.
        self._human_wait_enabled = bool(wait_for_human)
        # LLM enrichment (see _schedule_llm_enrichment). llm_service is resolved
        # lazily from settings on first need unless injected (tests).
        self._llm_service: Any | None = llm_service
        self._llm_resolved = llm_service is not None
        self._llm_off_reason: str | None = None
        self._llm_tasks: dict[int, asyncio.Task] = {}
        self._llm_sem: asyncio.Semaphore | None = None
        self._llm_calls = 0
        self._llm_failure_streak = 0
        self._already_harvested: set[str] = already_harvested or set()
        self._already_harvested_ids: set[str] = {
            jid for u in self._already_harvested if (jid := naukri_job_id_from_url(u))
        }
        self._skipped_already_harvested = 0
        # Pinned once per run so every relative "N days ago" label on a long run
        # resolves against the same reference point.
        self._harvest_started_at = datetime.now(timezone.utc)
        # Intercepted Naukri API responses: v3 search keyed by pageNo,
        # v4 job detail keyed by jobId.
        self._v3_by_page: dict[int, tuple[str, dict]] = {}
        self._v4_by_id: dict[str, dict] = {}
        self._details_enabled = True
        # Detail-pass pacing / block recovery state (re-armed per run in
        # _paginate_and_collect; built here too so helpers work stand-alone).
        self._reset_pacing(get_settings())
        # The current run's progress / status sinks (set by _paginate_and_collect)
        # so waits deep in the detail pass can report cooling_down / waiting_for_human.
        self._progress_cb: Callable[..., Awaitable[None]] | None = None
        self._status_cb: StatusCallback | None = None
        # Results-page URL the detail tabs are opened "from" (Referer).
        self._srp_url: str | None = None

    def _reset_pacing(self, settings: Any) -> None:
        self._governor = NaukriRateGovernor.from_settings(settings)
        self._breaker = NaukriCircuitBreaker.from_settings(settings)
        self._block_stats = NaukriBlockStats()
        self._last_human_wait_at: float | None = None
        # Set right before a detail load whose block would trip the breaker while
        # a human wait is possible: _fetch_detail then hands the challenged tab
        # back (on _PageBlocked.tab) instead of closing it.
        self._hold_blocked_tab = False

    # ── Public API ─────────────────────────────────────────────────────────────

    async def harvest(
        self,
        filters:  FiltersConfig,
        headless: bool = False,
        slow_mo:  int  = 0,
    ) -> list[NaukriScrapedJob]:
        """Standalone run (/run-naukri-agent and the legacy orchestrator.run()).
        Browser per settings.naukri_browser_mode: "shared" → the configured
        persistent Chrome profile; "isolated" → a fresh guest context;
        "recruiter" → the logged-in Naukri recruiter profile (never falls back
        to guest: an invalid session returns [])."""
        settings = get_settings()
        mode = settings.naukri_browser_mode
        logger.info(
            "naukri_search_started",
            keyword   = filters.keyword,
            location  = filters.location,
            work_mode = filters.work_mode,
            max_jobs  = filters.max_jobs,
            browser_mode = mode,
        )
        try:
            if mode == "isolated":
                async with naukri_guest_browser(headless=headless, slow_mo=slow_mo) as bm:
                    page = await bm.new_page()
                    jobs = await self._run(page, filters)
            elif mode == "recruiter":
                # Lazy: the recruiter-profile helpers live with the recruiter agent.
                from app.agents.naukri_recruiter_agent import (
                    check_recruiter_session,
                    login_window_open,
                    recruiter_harvest_browser,
                )
                if login_window_open():
                    # The profile is locked by the interactive login window.
                    logger.warning("naukri_recruiter_session_invalid", reason="login window open")
                    return []
                async with recruiter_harvest_browser(headless=headless, slow_mo=slow_mo) as rbm:
                    page = await rbm.acquire_page(label="naukri")
                    ok, detail = await check_recruiter_session(page)
                    if not ok:
                        logger.warning("naukri_recruiter_session_invalid", reason=detail)
                        return []
                    jobs = await self._run(page, filters)
            else:
                from app.services.config_service import ConfigService
                chrome_profile = ConfigService().load().browser.chrome_profile
                async with PersistentBrowserManager(
                    profile_dir = chrome_profile,
                    headless    = headless,
                    slow_mo     = slow_mo,
                ) as pbm:
                    page = await pbm.acquire_page(label="naukri")
                    jobs = await self._run(page, filters)
            logger.info("agent_completed", source="naukri", total=len(jobs))
            return jobs
        except Exception as exc:
            logger.exception("agent_failed", source="naukri", error=str(exc))
            return []

    def get_token_usage(self) -> dict:
        """Cumulative token usage of this run's Naukri LLM enrichment calls
        (zeroed summary when the LLM was never used)."""
        if self._llm_service is None or not hasattr(self._llm_service, "get_usage_summary"):
            from app.services.llm_service import empty_usage_summary
            return empty_usage_summary()
        return self._llm_service.get_usage_summary()

    def get_llm_call_log(self) -> list[dict]:
        """Per-call audit log of this run's Naukri LLM enrichment calls."""
        if self._llm_service is None or not hasattr(self._llm_service, "get_call_log"):
            return []
        return self._llm_service.get_call_log()

    # ── LLM enrichment (on top of the deterministic parse) ──────────────────────
    #
    # Placement: each job's LLM work starts as a background task right after its
    # detail page is parsed, so the call overlaps the next detail visits and the
    # governed 30–60 s gaps between them (≈ zero added wall time). Before a
    # batch is handed to on_batch (persisted), its jobs' tasks are awaited with a
    # hard cap, so the persisted row already carries the LLM result. Bounded by:
    # per-call timeout (naukri_llm_timeout_s), a concurrency semaphore
    # (naukri_llm_concurrency), a per-run call cap, and a failure circuit
    # breaker. Any LLM error leaves the deterministic values in place.

    def _get_llm(self, settings: Any) -> Any | None:
        if self._llm_off_reason:
            return None
        if not self._llm_resolved:
            self._llm_resolved = True
            settings = naukri_llm_settings(settings)
            if not llm_configured(settings):
                self._llm_off_reason = "llm_not_configured"
                logger.warning(
                    "naukri_llm_enrichment_skipped",
                    reason="no LLM provider configured (EXTRACTION_LLM_MODEL key/URL missing)",
                    note="deterministic contact extraction / formatting only",
                )
                return None
            from app.services.llm_service import LLMService
            self._llm_service = LLMService(settings)
        return self._llm_service

    def _llm_budget_left(self, settings: Any) -> bool:
        cap = int(getattr(settings, "naukri_llm_max_calls_per_run", 0) or 0)
        if cap and self._llm_calls >= cap:
            if self._llm_off_reason is None:
                self._llm_off_reason = "call_cap_reached"
                logger.warning("naukri_llm_call_cap_reached", cap=cap)
            return False
        return True

    def _schedule_llm_enrichment(self, job: NaukriScrapedJob) -> None:
        """Start LLM enrichment for ``job`` in the background when it can help:
        contact extraction when the description shows a contact signal,
        restructuring when it is a flat run-on blob. No-op otherwise."""
        try:
            settings = get_settings()
            want_contacts = bool(settings.naukri_llm_contact_extraction) and has_contact_signals(
                job.job_description, job.description_source_html,
            )
            want_restructure = bool(settings.naukri_llm_description_restructure) and \
                is_flat_description(job.job_description)
            if not (want_contacts or want_restructure):
                return
            llm = self._get_llm(settings)
            if llm is None or not self._llm_budget_left(settings):
                return
            if self._llm_sem is None:
                self._llm_sem = asyncio.Semaphore(max(1, int(settings.naukri_llm_concurrency)))
            timeout_s = float(settings.naukri_llm_timeout_s)
            self._llm_tasks[id(job)] = asyncio.create_task(
                self._llm_enrich(job, llm, want_contacts, want_restructure, timeout_s, settings)
            )
        except Exception as exc:  # scheduling must never break the scrape
            logger.warning("naukri_llm_schedule_failed", url=job.job_url, error=str(exc))

    def _llm_failed(self, exc: BaseException, what: str, ref: str) -> None:
        self._llm_failure_streak += 1
        logger.warning(
            "naukri_llm_enrichment_failed", what=what, job=ref,
            error=str(exc) or type(exc).__name__, streak=self._llm_failure_streak,
            note="deterministic result kept",
        )
        if self._llm_failure_streak >= self._LLM_MAX_FAILURE_STREAK and self._llm_off_reason is None:
            self._llm_off_reason = "repeated_failures"
            logger.warning(
                "naukri_llm_enrichment_disabled", reason="repeated_failures",
                streak=self._llm_failure_streak,
                note="deterministic results stand for the rest of this run",
            )

    async def _llm_enrich(
        self, job: NaukriScrapedJob, llm: Any, want_contacts: bool, want_restructure: bool,
        timeout_s: float, settings: Any,
    ) -> None:
        assert self._llm_sem is not None
        async with self._llm_sem:
            ref = job.job_url or job.job_id
            text, src_html = job.job_description, job.description_source_html
            if want_contacts and not self._llm_off_reason and self._llm_budget_left(settings):
                self._llm_calls += 1
                try:
                    grounded = await llm_extract_contacts(
                        llm, text,
                        sources=[text, html_contact_text(src_html), "\n".join(mailto_addresses(src_html))],
                        link_targets=[f"mailto:{a}" for a in mailto_addresses(src_html)]
                        + [f"tel:{t}" for t in tel_numbers(src_html)],
                        job_ref=ref, timeout_s=timeout_s, email_filter=_clean_email,
                    )
                    changed = apply_llm_contacts(job, grounded)
                    self._llm_failure_streak = 0
                    if changed:
                        logger.info("naukri_llm_contacts_applied", job=ref, fields=changed)
                except Exception as exc:
                    self._llm_failed(exc, "contacts", ref)
            if want_restructure and not self._llm_off_reason and self._llm_budget_left(settings):
                self._llm_calls += 1
                try:
                    restructured = await llm_restructure_description(
                        llm, text, job_ref=ref, timeout_s=timeout_s,
                    )
                    self._llm_failure_streak = 0
                    if restructured:
                        job.job_description = restructured
                        job.job_description_html = description_text_to_html(restructured)
                        logger.info("naukri_llm_description_restructured", job=ref)
                except Exception as exc:
                    self._llm_failed(exc, "restructure", ref)

    async def _drain_llm(self, jobs: list[NaukriScrapedJob] | None = None) -> None:
        """Wait (bounded) for the LLM tasks of ``jobs`` (all when None). Tasks
        still running after the cap are cancelled — their jobs keep the
        deterministic values. Never raises."""
        if not self._llm_tasks:
            return
        keys = list(self._llm_tasks) if jobs is None else [id(j) for j in jobs if id(j) in self._llm_tasks]
        tasks = [self._llm_tasks.pop(k) for k in keys]
        if not tasks:
            return
        try:
            cap_s = max(5.0, float(get_settings().naukri_llm_timeout_s) * 3)
            _done, pending = await asyncio.wait(tasks, timeout=cap_s)
            if pending:
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                logger.warning("naukri_llm_drain_cap_hit", cancelled=len(pending), cap_s=cap_s)
        except Exception as exc:
            logger.warning("naukri_llm_drain_failed", error=str(exc))

    # ── Internal flow ──────────────────────────────────────────────────────────

    async def _run(
        self,
        page: Page,
        f: FiltersConfig,
        on_status: StatusCallback | None = None,
        on_batch: BatchCallback | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> list[NaukriScrapedJob]:
        """Harvest Naukri for `f` on `page` (guest — no login step).

        on_status   optional async callback for human-readable progress messages.
        on_batch    optional async callback fired with each batch of freshly
                    scraped jobs (every settings.harvest_persist_batch_size) so the
                    caller can persist incrementally — crash safety + live progress.
        on_progress optional async callback with structured live progress (phase,
                    reported_total, listed, skipped, target, done, in_batch, …) —
                    after each page's listing/dedupe and once per extracted job.
                    Best-effort: a failing callback never affects scraping.
        """
        page.on("response", self._on_response)
        try:
            jobs = await self._paginate_and_collect(
                page, f, on_status=on_status, on_batch=on_batch, on_progress=on_progress,
            )
        finally:
            try:
                page.remove_listener("response", self._on_response)
            except Exception:
                pass
            self._progress_cb = None
            self._status_cb = None
            # Only left over when pagination raised — never leak background tasks.
            for task in self._llm_tasks.values():
                task.cancel()
            self._llm_tasks.clear()

        logger.info(
            "naukri_jobs_extracted", total=len(jobs),
            skipped_already_harvested=self._skipped_already_harvested,
        )
        if not jobs:
            return jobs

        settings = get_settings()
        # Company-level enrichment (size + HQ) — the shared, browser-free Apollo
        # pass LinkedIn uses; cached on CompanyORM. Best-effort: never loses jobs.
        try:
            await _status(on_status, f"Naukri: enriching companies for {len(jobs)} jobs")
            from app.services.company_enrichment_service import enrich_companies
            await enrich_companies(jobs, settings)
        except Exception as exc:
            logger.warning("naukri_company_enrichment_pass_failed", error=str(exc))

        # Company+location HR-contact fallback for jobs still without an email —
        # same pass as LinkedIn (writes only its cache; attached at insert time).
        try:
            from app.services.company_location_contact_service import (
                discover_company_location_contacts,
            )
            await discover_company_location_contacts(jobs, settings)
        except Exception as exc:
            logger.warning("naukri_company_location_contact_pass_failed", error=str(exc))

        return jobs

    async def _on_response(self, response: Any) -> None:
        """Capture Naukri's own search (v3) / job-detail (v4) JSON as the page
        loads it. Never raises into Playwright's event loop."""
        try:
            url = response.url
            if "/jobapi/v3/search" in url:
                if response.status != 200:
                    return
                q = parse_qs(urlparse(url).query)
                try:
                    page_no = int((q.get("pageNo") or ["1"])[0])
                except ValueError:
                    page_no = 1
                body = await response.json()
                if isinstance(body, dict):
                    self._v3_by_page[page_no] = (url, body)
            elif "/jobapi/v4/job/" in url:
                if response.status != 200:
                    return
                m = re.search(r"/jobapi/v4/job/(\d+)", url)
                body = await response.json()
                if m and isinstance(body, dict):
                    self._v4_by_id[m.group(1)] = body
        except Exception as exc:
            logger.debug("naukri_response_capture_failed", error=str(exc))

    async def _await_capture(self, page: Page, check: Callable[[], Any], timeout_ms: int) -> Any:
        waited = 0
        while waited < timeout_ms:
            if (val := check()):
                return val
            await page.wait_for_timeout(250)
            waited += 250
        return check()

    async def _paginate_and_collect(
        self,
        page: Page,
        f: FiltersConfig,
        on_status: StatusCallback | None = None,
        on_batch: BatchCallback | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> list[NaukriScrapedJob]:
        """Page through the SRP collecting jobs (listing + detail) until the cap,
        the end of results, an unrecoverable block/CAPTCHA, a filter loss, or a
        user stop. Detail visits are paced by the rate governor; blocks cool down
        and retry (see _detail_with_recovery)."""
        settings = get_settings()
        self._reset_pacing(settings)
        safety_cap = f.max_jobs if f.max_jobs > 0 else 5_000
        test_cap = env_max_jobs_cap(_TEST_MAX_JOBS_ENV)
        if test_cap is not None:
            logger.warning(
                "naukri_test_mode_max_jobs_active", test_cap=test_cap,
                configured_max_jobs=f.max_jobs,
                hint=f"{_TEST_MAX_JOBS_ENV} is set in .env — remove it to harvest normally.",
            )
            safety_cap = min(safety_cap, test_cap)
        self._details_enabled = bool(settings.naukri_fetch_details)

        all_jobs: list[NaukriScrapedJob] = []
        seen_ids: set[str] = set()
        empty_pages = 0
        total_pages: int | None = None
        page_num = 1

        persist_n = max(1, settings.harvest_persist_batch_size)
        pending: list[NaukriScrapedJob] = []

        async def _flush_pending() -> None:
            # LLM enrichment of these jobs must land before they are persisted.
            if pending:
                await self._drain_llm(list(pending))
            if on_batch and pending:
                batch = list(pending)
                pending.clear()
                try:
                    await on_batch(batch)
                except Exception as exc:
                    logger.warning("naukri_on_batch_failed", count=len(batch), error=str(exc))

        # Structured live progress (Live Harvest card). The full state is sent on
        # every call; the tracker persists only what changed (ticks throttled).
        prog: dict[str, Any] = {
            "phase": "listing", "reported_total": None, "listed": 0, "skipped": 0,
            "cap": safety_cap, "batch_size": persist_n,
            "window_hours": f.search_window_hours, "page": page_num, "total_pages": None,
        }

        async def _progress(message: str | None = None, **fields: Any) -> None:
            if on_progress is None:
                return
            try:
                prog.update(fields)
                prog["skipped"] = self._skipped_already_harvested
                found = prog["reported_total"] if prog["reported_total"] is not None else prog["listed"]
                prog["target"] = min(safety_cap, max(len(all_jobs), int(found) - prog["skipped"]))
                prog["done"] = len(all_jobs)
                prog["in_batch"] = len(pending)
                payload = dict(prog)
                if message:
                    payload["message"] = message
                await on_progress(payload)
            except Exception as exc:  # progress reporting must never break a scrape
                logger.debug("naukri_progress_callback_failed", error=str(exc))

        # Waits inside the detail pass report through the same sinks.
        self._progress_cb = _progress
        self._status_cb = on_status

        logger.info(
            "search_started", source="naukri", keyword=_keyword_for(f), location=f.location,
            work_mode=f.work_mode, job_type=f.job_type, max_jobs=f.max_jobs,
            search_window_hours=f.search_window_hours, safety_cap=safety_cap,
        )
        await _status(on_status, "Naukri: searching…")

        srp_retried_page: int | None = None   # results page already retried after a block
        while len(all_jobs) < safety_cap and page_num <= _MAX_PAGES:
            if run_guard.is_stop_requested():
                logger.info("naukri_pagination_stopped_by_user", page=page_num, collected=len(all_jobs))
                break
            if total_pages is not None and page_num > total_pages:
                logger.info("naukri_results_exhausted", page=page_num, total_pages=total_pages)
                break
            # No Naukri request at all while the breaker is OPEN.
            if not await self._breaker_pause_if_open(page, resume_phase="listing"):
                break

            await _progress(phase="listing", page=page_num)
            try:
                listing, search_req_url = await self._load_results_page(page, f, page_num)
            except SearchFilterNotAppliedException:
                if page_num == 1:
                    raise
                logger.warning("naukri_filter_lost_stopping", page=page_num, collected=len(all_jobs))
                break
            except _PageBlocked as exc:
                self._block_stats.srp_blocks += 1
                self._governor.on_block()
                retried = srp_retried_page == page_num
                logger.warning("naukri_page_blocked", page=page_num, reason=str(exc),
                               collected=len(all_jobs), retried=retried)
                # One recovery (human wait or cooldown) + retry per results page;
                # a second block on the same page ends pagination.
                if retried or not await self._recover_srp_block(page, page_num):
                    break
                srp_retried_page = page_num
                continue
            except Exception as exc:
                if page_num == 1:
                    logger.error("naukri_navigation_failed", page=page_num, error=str(exc))
                    raise
                logger.warning("naukri_page_failed", page=page_num, error=str(exc), collected=len(all_jobs))
                break

            if page_num == 1:
                v3 = self._v3_by_page.get(1)
                no_of_jobs = v3[1].get("noOfJobs") if v3 else None
                if isinstance(no_of_jobs, int):
                    total_pages = max(1, math.ceil(no_of_jobs / _PAGE_SIZE))
                logger.info(
                    "naukri_search_diagnostics", search_request=search_req_url,
                    final_url=page.url, total_jobs_reported=no_of_jobs, total_pages=total_pages,
                )
                await _status(
                    on_status,
                    f"Naukri: {no_of_jobs if no_of_jobs is not None else 'unknown number of'} matching jobs",
                )
                await _progress(
                    reported_total=no_of_jobs if isinstance(no_of_jobs, int) else None,
                    total_pages=total_pages,
                )

            if not listing:
                empty_pages += 1
                logger.info("naukri_page_empty", page=page_num, consecutive_empty=empty_pages)
                if empty_pages >= 2 or total_pages is not None:
                    break
                page_num += 1
                continue
            empty_pages = 0

            # Phase A: dedup / skip BEFORE any detail visit (skips don't use the cap).
            remaining = safety_cap - len(all_jobs)
            to_fetch: list[NaukriScrapedJob] = []
            for job in listing:
                if len(to_fetch) >= remaining:
                    break
                key = job.job_id or normalize_job_url(job.job_url)
                if not key or key in seen_ids:
                    continue
                if (normalize_job_url(job.job_url) in self._already_harvested
                        or (job.job_id and job.job_id in self._already_harvested_ids)):
                    self._skipped_already_harvested += 1
                    seen_ids.add(key)
                    continue
                seen_ids.add(key)
                to_fetch.append(job)
            await _progress(
                phase="details" if to_fetch else "listing",
                listed=int(prog["listed"] or 0) + len(listing),
                message=f"Naukri: page {page_num} — {len(to_fetch)} new of {len(listing)} listed",
            )

            # Phase B: per-job detail (isolated per job), then incremental flush.
            # Each detail visit is preceded by the governed gap (or a break); a
            # blocked visit cools down / retries inside _detail_with_recovery. A
            # job whose detail never loads is still kept with its listing data.
            for idx, job in enumerate(to_fetch):
                if run_guard.is_stop_requested():
                    logger.info("naukri_detail_loop_stopped_by_user", collected=len(all_jobs))
                    break
                if self._details_enabled:
                    if not await self._pace_before_detail(page):
                        logger.info("naukri_detail_loop_stopped_by_user", collected=len(all_jobs))
                        break
                    await self._detail_with_recovery(page, job)
                # Background LLM enrichment (contacts / flat-description repair);
                # overlaps the next gap and detail visits.
                self._schedule_llm_enrichment(job)
                all_jobs.append(job)
                pending.append(job)
                logger.info("job_card_extracted", source="naukri", title=job.job_title,
                            company=job.company, url=job.job_url)
                await _progress(
                    message=f"Page {page_num} · detail {idx + 1} of {len(to_fetch)} fetched",
                )
                if len(pending) >= persist_n:
                    await _flush_pending()
            await _flush_pending()

            logger.info(
                "naukri_page_done", page=page_num, listed=len(listing), new=len(to_fetch),
                total=len(all_jobs), skipped_already_harvested=self._skipped_already_harvested,
            )
            await _status(on_status, f"Naukri: page {page_num} done — {len(all_jobs)} jobs collected")

            if run_guard.is_stop_requested():
                break
            page_num += 1
            if len(all_jobs) >= safety_cap or (total_pages is not None and page_num > total_pages):
                continue                              # loop ends — no pointless wait
            gap_s = random.uniform(8.0, 14.0) if page_num % 10 == 0 else random.uniform(3.0, 6.0)
            if not self._details_enabled:
                # No detail gaps to slow the run down — pace the results pages
                # with the governor instead of going full speed.
                gap_s = max(gap_s, self._governor.next_gap_s())
            if not await self._wait(page, gap_s):
                break

        await _flush_pending()
        await self._drain_llm()
        await _progress(phase="done", resume_at=0)
        self._block_stats.final_multiplier = self._governor.multiplier
        block_summary = self._block_stats.as_log()
        logger.info(
            "pagination_completed", source="naukri", pages_visited=page_num,
            total_jobs=len(all_jobs), skipped_already_harvested=self._skipped_already_harvested,
            **block_summary,
        )
        logger.info("naukri_block_summary", **block_summary)
        return all_jobs

    async def _load_results_page(
        self, page: Page, f: FiltersConfig, page_num: int,
    ) -> tuple[list[NaukriScrapedJob], str]:
        """Navigate to SRP page `page_num`, verify filters, and return its jobs
        (+ the search-request URL used for verification)."""
        search_url = build_search_url(f, page_no=page_num)
        logger.info("generated_search_url", url=search_url, page=page_num)

        for attempt in (1, 2):
            self._v3_by_page.pop(page_num, None)
            await retry_async(
                lambda: page.goto(search_url, wait_until="domcontentloaded", timeout=30_000),
                retries=3, delay_s=3.0,
            )
            await self._raise_if_blocked(page, page_num)
            self._srp_url = page.url or search_url
            v3 = await self._await_capture(page, lambda: self._v3_by_page.get(page_num), 15_000)
            req_url = v3[0] if v3 else page.url
            missing = missing_search_filters(req_url, f, page_num)
            if not missing:
                break
            logger.warning(
                "search_filter_not_applied", page=page_num, attempt=attempt,
                missing=missing, generated_url=search_url, request_url=req_url,
            )
            if attempt == 2:
                await self._screenshot(page, f"naukri_filter_missing_p{page_num}")
                raise SearchFilterNotAppliedException(
                    f"Naukri did not apply {missing} on page {page_num} "
                    f"(requested {search_url}; Naukri searched {req_url})"
                )
            await _delay(page, 2_000, 4_000)

        await self._dismiss_overlays(page)
        # Brief human-like scroll through the result list (also triggers any
        # lazy rendering the DOM fallback needs).
        await human_scroll(page, max_rounds=8, pause_lo=250, pause_hi=700)

        ref = self._harvest_started_at
        jobs: list[NaukriScrapedJob] = []
        method = "v3_json"
        if v3:
            for d in v3[1].get("jobDetails") or []:
                try:
                    if (job := job_from_search_json(d, ref)) is not None:
                        jobs.append(job)
                except Exception as exc:
                    logger.debug("naukri_v3_card_skipped", error=str(exc))
        if not jobs:
            method = "dom"
            try:
                await page.wait_for_selector("[data-job-id]", timeout=8_000)
            except Exception:
                pass
            try:
                raw = await page.evaluate(_DOM_CARDS_JS)
            except Exception as exc:
                logger.warning("naukri_dom_cards_failed", page=page_num, error=str(exc))
                raw = []
            for d in raw or []:
                try:
                    if (job := job_from_dom_card(d, ref)) is not None:
                        jobs.append(job)
                except Exception as exc:
                    logger.debug("naukri_dom_card_skipped", error=str(exc))
            if not jobs and page_num == 1:
                await self._screenshot(page, "naukri_no_cards")
        logger.info("naukri_cards_found", page=page_num, count=len(jobs), method=method)
        return jobs, req_url

    # ── Detail pass: pacing, block recovery, human-in-the-loop ─────────────────
    #
    # Contract with the Live Harvest card (on_progress payload):
    #   phase="cooling_down"      a timed pause (pacing break, block cooldown,
    #                             breaker pause); resume_at = epoch s it ends
    #   phase="waiting_for_human" a CAPTCHA tab is held open in the Live Browser;
    #                             resume_at = the wait's deadline
    #   back to "details" / "listing" with resume_at=0 when the pause ends.

    async def _emit_progress(self, message: str | None = None, **fields: Any) -> None:
        cb = self._progress_cb
        if cb is None:
            return
        try:
            await cb(message, **fields)
        except Exception as exc:  # progress reporting must never break a scrape
            logger.debug("naukri_progress_callback_failed", error=str(exc))

    async def _wait(
        self,
        page: Any | None,
        seconds: float,
        *,
        phase: str | None = None,
        message: str | None = None,
        resume_phase: str = "details",
        cooldown: bool = False,
    ) -> bool:
        """Cooperative wait of ``seconds`` in ≤ _WAIT_CHUNK_S slices, ending early
        on a user stop. With ``phase`` it reports that phase (+ resume_at) first
        and ``resume_phase`` (resume_at=0) when the wait completes. ``cooldown``
        counts the time towards the block summary. False when stopped."""
        total = max(0.0, float(seconds or 0))
        if phase:
            await self._emit_progress(message, phase=phase, resume_at=int(_wall_time() + total))
        waited = 0.0
        stopped = False
        while waited < total:
            if run_guard.is_stop_requested():
                stopped = True
                break
            chunk = min(_WAIT_CHUNK_S, total - waited)
            await _sleep_chunk(page, chunk)
            waited += chunk
        if cooldown:
            self._block_stats.cooldown_seconds_total += waited
        stopped = stopped or run_guard.is_stop_requested()
        if phase and not stopped:
            await self._emit_progress(
                _RESUME_MESSAGES.get(resume_phase), phase=resume_phase, resume_at=0,
            )
        return not stopped

    async def _pace_before_detail(self, page: Page) -> bool:
        """The governed gap before a detail visit (none before the run's first),
        or a longer pacing break every naukri_break_every_details visits. False
        when the user stopped the run during the wait."""
        gov = self._governor
        if gov.visits == 0:
            return not run_guard.is_stop_requested()
        if gov.break_due():
            secs = gov.break_s()
            gov.rested()
            self._block_stats.breaks += 1
            logger.info("naukri_pacing_break", after_details=gov.visits, seconds=int(secs),
                        multiplier=gov.multiplier)
            return await self._wait(
                page, secs, phase="cooling_down",
                message=f"Naukri: pacing break — resuming in ~{int(secs)}s",
            )
        return await self._wait(page, gov.next_gap_s())

    def _human_wait_possible(self) -> bool:
        """A human may be asked to solve a CAPTCHA: opted in by the caller,
        naukri_human_wait_s > 0, and no other human wait in the last 30 min."""
        if not self._human_wait_enabled:
            return False
        if int(get_settings().naukri_human_wait_s or 0) <= 0:
            return False
        last = self._last_human_wait_at
        return last is None or _monotonic() - last >= _HUMAN_WAIT_SPACING

    async def _breaker_pause_if_open(self, page: Page, resume_phase: str = "details") -> bool:
        """While the breaker is OPEN: pause (cooling_down), then HALF_OPEN so the
        next detail load is the probe. False when stopped during the pause."""
        br = self._breaker
        if br.state != br.OPEN:
            return True
        secs = br.pause_s()
        logger.warning("naukri_circuit_pause", seconds=int(secs), reopens=br.reopens,
                       multiplier=self._governor.multiplier)
        ok = await self._wait(
            page, secs, phase="cooling_down",
            message=f"Naukri: rate-limited repeatedly — pausing, resuming in ~{int(secs)}s",
            resume_phase=resume_phase, cooldown=True,
        )
        if not ok:
            return False
        self._governor.rested()
        br.half_open()
        logger.info("naukri_circuit_half_open")
        return True

    async def _cooldown(self, page: Page, resume_phase: str, **log: Any) -> bool:
        """The post-block cooldown (uniform naukri_block_cooldown_min/max_s)."""
        settings = get_settings()
        lo = max(0, int(settings.naukri_block_cooldown_min_s))
        secs = random.uniform(lo, max(lo, int(settings.naukri_block_cooldown_max_s)))
        logger.info("naukri_block_cooldown", seconds=int(secs), **log)
        ok = await self._wait(
            page, secs, phase="cooling_down",
            message=f"Naukri: rate-limited — cooling down, resuming in ~{int(secs)}s",
            resume_phase=resume_phase, cooldown=True,
        )
        if ok:
            self._governor.rested()
        return ok

    async def _detail_with_recovery(self, page: Page, job: NaukriScrapedJob) -> None:
        """One job's detail visit with block recovery. Never raises; on give-up
        the job keeps its listing data.

        blocked        → cooldown → retry the same job once
        breaker trips  → (human wait when allowed) → timed pause at the next
                         attempt → HALF_OPEN probe (this job's retry, or the next job)
        """
        retried = False
        while True:
            if run_guard.is_stop_requested():
                return
            if not await self._breaker_pause_if_open(page):
                return
            self._hold_blocked_tab = self._human_wait_possible() and self._breaker.would_trip()
            self._governor.note_visit()
            blocked: _PageBlocked | None = None
            try:
                await self._fetch_detail(page, job)
            except _PageBlocked as exc:
                blocked = exc
            except Exception as exc:
                logger.warning("naukri_detail_failed", url=job.job_url, error=str(exc))
                if retried:
                    self._block_stats.retries_failed += 1
                return
            finally:
                self._hold_blocked_tab = False

            if blocked is None:
                was = self._breaker.state
                self._governor.on_clean()
                self._breaker.on_success()
                if was != NaukriCircuitBreaker.CLOSED:
                    logger.info("naukri_circuit_closed", probe_job_id=job.job_id)
                if retried:
                    self._block_stats.retries_ok += 1
                    logger.info("naukri_detail_retry_ok", job_id=job.job_id)
                return

            tab = blocked.tab
            self._block_stats.detail_blocks += 1
            self._governor.on_block()
            tripped = self._breaker.on_block()
            logger.warning(
                "naukri_detail_blocked", url=job.job_url, job_id=job.job_id, reason=str(blocked),
                streak=self._breaker.consecutive, retry=retried,
                multiplier=self._governor.multiplier, breaker=self._breaker.state,
            )
            solved = False
            if tripped:
                self._block_stats.breaker_trips += 1
                logger.warning("naukri_circuit_open", consecutive_blocks=self._breaker.consecutive,
                               trips=self._breaker.trips, reopens=self._breaker.reopens)
                if tab is not None and self._human_wait_possible():
                    solved = await self._await_human_solve(tab, label=job.job_id or job.job_url)
                    if solved:
                        try:
                            await self._extract_detail(tab, job)
                        except Exception as exc:
                            logger.warning("naukri_detail_failed", url=job.job_url, error=str(exc))
                        # Human cleared the wall — CLOSED again; the governor
                        # stays slowed down.
                        self._breaker.on_success()
            await self._discard_tab(page, tab)
            if solved:
                return
            if retried:
                self._block_stats.retries_failed += 1
                logger.warning("naukri_detail_gave_up", url=job.job_url, job_id=job.job_id,
                               note="kept with listing data")
                return
            # Tripped: the breaker pause runs at the top of the next attempt.
            if not tripped and not await self._cooldown(page, "details", job_id=job.job_id):
                return
            retried = True

    async def _recover_srp_block(self, page: Page, page_num: int) -> bool:
        """A results page was blocked: a human wait when allowed (the SRP tab is
        the challenged tab), else / then a cooldown. True → retry the page."""
        if self._human_wait_possible():
            if await self._await_human_solve(page, label=f"p{page_num}", resume_phase="listing"):
                return True
            if run_guard.is_stop_requested():
                return False
        return await self._cooldown(page, "listing", srp_page=page_num)

    async def _await_human_solve(self, tab: Any, *, label: str, resume_phase: str = "details") -> bool:
        """Hold the challenged tab in front in the Live Browser and poll (every
        _HUMAN_POLL_S) until it is no longer blocked — a person ticking the box
        makes Naukri load the real page — for at most naukri_human_wait_s.
        True when solved; False on timeout / stop. Never solves anything itself."""
        limit_s = float(max(0, int(get_settings().naukri_human_wait_s or 0)))
        self._last_human_wait_at = _monotonic()
        self._block_stats.human_waits += 1
        msg = (
            "Naukri needs you — solve the CAPTCHA in the Live Browser "
            f"(waiting up to {max(1, math.ceil(limit_s / 60))} min)"
        )
        try:
            await tab.bring_to_front()
        except Exception:
            pass
        logger.warning("naukri_waiting_for_human", label=label, wait_s=int(limit_s),
                       url=getattr(tab, "url", ""))
        await _status(self._status_cb, msg)
        await self._emit_progress(msg, phase="waiting_for_human",
                                  resume_at=int(_wall_time() + limit_s))
        waited = 0.0
        clean_polls = 0
        while waited < limit_s and not run_guard.is_stop_requested():
            slept = 0.0
            while slept < _HUMAN_POLL_S and waited < limit_s:
                if run_guard.is_stop_requested():
                    break
                chunk = min(_WAIT_CHUNK_S, _HUMAN_POLL_S - slept, limit_s - waited)
                await _sleep_chunk(tab, chunk)
                slept += chunk
                waited += chunk
            if run_guard.is_stop_requested():
                break
            # Two clean checks in a row, so a blank mid-navigation frame isn't
            # mistaken for a solved challenge.
            if await self._block_reason(tab) is None:
                clean_polls += 1
                if clean_polls >= 2:
                    self._block_stats.human_solved += 1
                    logger.info("naukri_captcha_solved_by_human", label=label, waited_s=int(waited))
                    await self._emit_progress(
                        _RESUME_MESSAGES.get(resume_phase), phase=resume_phase, resume_at=0,
                    )
                    return True
            else:
                clean_polls = 0
        logger.warning("naukri_human_wait_timeout", label=label, waited_s=int(waited),
                       stopped=run_guard.is_stop_requested())
        return False

    # ── Detail page: click-through tab + extraction ────────────────────────────

    async def _fetch_detail(self, page: Page, job: NaukriScrapedJob) -> None:
        """Enrich `job` from its detail page, opened like a click-through from
        the results page: a NEW tab of the same context with the SRP as Referer
        (falls back to the SRP tab itself if a tab can't be opened); a short
        reading scroll; then v4 JSON → ld+json → DOM. The SRP tab stays put.

        Raises _PageBlocked on a CAPTCHA / bot wall — with the challenged tab
        left open on ``exc.tab`` when self._hold_blocked_tab is set (human wait),
        otherwise the tab is closed."""
        if job.job_id:
            self._v4_by_id.pop(job.job_id, None)
        referer = self._srp_url or (page.url or None)
        tab = await self._open_detail_tab(page)
        target = tab if tab is not None else page
        held = False
        try:
            await retry_async(lambda: self._goto(target, job.job_url, referer), retries=2, delay_s=2.0)
            try:
                await self._raise_if_blocked(target, 0, job_id=job.job_id)
            except _PageBlocked as exc:
                if self._hold_blocked_tab:
                    exc.tab = target
                    held = True
                raise
            await self._reading_scroll(target)
            await self._extract_detail(target, job)
        finally:
            if tab is not None and not held:
                await self._close_tab(tab)

    @staticmethod
    async def _goto(target: Any, url: str, referer: str | None) -> Any:
        kwargs: dict[str, Any] = {"wait_until": "domcontentloaded", "timeout": 30_000}
        if referer:
            kwargs["referer"] = referer
        return await target.goto(url, **kwargs)

    async def _open_detail_tab(self, page: Page) -> Any | None:
        """A new tab in the SRP's context with the response capture attached;
        None when one can't be opened (caller falls back to the SRP tab)."""
        try:
            tab = await page.context.new_page()
        except Exception as exc:
            logger.debug("naukri_detail_tab_open_failed", error=str(exc), note="same-tab fallback")
            return None
        try:
            tab.on("response", self._on_response)
        except Exception:
            pass
        return tab

    async def _close_tab(self, tab: Any) -> None:
        try:
            tab.remove_listener("response", self._on_response)
        except Exception:
            pass
        try:
            await tab.close()
        except Exception as exc:
            logger.debug("naukri_detail_tab_close_failed", error=str(exc))

    async def _discard_tab(self, page: Page, tab: Any | None) -> None:
        """Close a held detail tab — never the SRP tab itself."""
        if tab is None or tab is page:
            return
        await self._close_tab(tab)

    @staticmethod
    async def _reading_scroll(target: Any) -> None:
        """A few small, slow scroll steps — someone skimming the job page."""
        try:
            await human_scroll(target, max_rounds=3, step_min=250, step_max=600,
                               pause_lo=600, pause_hi=1_500)
        except Exception:
            pass

    async def _extract_detail(self, target: Any, job: NaukriScrapedJob) -> None:
        """v4 JSON (captured on ``target``) → ld+json → DOM description."""
        payload = None
        if job.job_id:
            payload = await self._await_capture(target, lambda: self._v4_by_id.get(job.job_id), 10_000)
        if payload and apply_detail_json(job, payload):
            logger.debug("naukri_detail_source", source="v4_json", job_id=job.job_id)
            return
        try:
            fb = await target.evaluate(_DETAIL_FALLBACK_JS)
        except Exception as exc:
            logger.debug("naukri_detail_fallback_eval_failed", error=str(exc))
            return
        if apply_ld_json(job, fb.get("ld") or []):
            logger.debug("naukri_detail_source", source="ld_json", job_id=job.job_id)
        elif apply_dom_description(job, fb.get("desc_html") or ""):
            logger.debug("naukri_detail_source", source="dom", job_id=job.job_id)
        else:
            logger.debug("naukri_detail_source", source="none", job_id=job.job_id)

    # ── Block / CAPTCHA detection ──────────────────────────────────────────────

    async def _block_reason(self, page: Any) -> str | None:
        """Why ``page`` is a wall rather than content, or None. No side effects
        (also used to poll a human-held CAPTCHA tab)."""
        url = getattr(page, "url", "") or ""
        title = ""
        try:
            title = await page.title()
        except Exception:
            pass
        if any(p in url for p in ("/challenge", "/authwall", "/nlogin/login")):
            return f"redirected to {url}"
        if "access denied" in title.lower():
            return "Access Denied (edge bot wall)"
        if await self._is_hard_captcha(page):
            return "captcha"
        return None

    async def _raise_if_blocked(self, page: Page, page_num: int, job_id: str | None = None) -> None:
        reason = await self._block_reason(page)
        if reason is None:
            return
        await self._save_captcha_artifacts(page, page_num, job_id=job_id, reason=reason)
        raise _PageBlocked(reason)

    async def _is_hard_captcha(self, page: Page) -> bool:
        """Detect an explicit user-facing CAPTCHA challenge in the main frame."""
        hard_sels = [
            "#challenge-form",
            "iframe[src*='challenges.cloudflare.com']",
            "iframe[src*='google.com/recaptcha/api2']",
            "iframe[src*='recaptcha.net/recaptcha']",
            "iframe[title*='reCAPTCHA']",
        ]
        for sel in hard_sels:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    return True
            except Exception:
                continue
        try:
            body = (await page.inner_text("body")).lower()
            if "i'm not a robot" in body or "check the box to let us know you" in body:
                return True
        except Exception:
            pass
        return False

    async def _save_captcha_artifacts(
        self, page: Page, page_num: int, job_id: str | None = None, reason: str = "captcha",
    ) -> dict:
        """Save a timestamped screenshot + HTML of the wall
        (naukri_captcha_<UTC yyyymmdd_HHMMSS>_<jobid|p{page}>.png/.html) plus a
        latest-copy naukri_captcha.png/.html. Returns the paths dict."""
        paths: dict = {}
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        tag = re.sub(r"[^A-Za-z0-9_-]", "", job_id or "")[:40] or f"p{page_num}"
        stem = f"naukri_captcha_{ts}_{tag}"
        try:
            _DEBUG_SCREENSHOT.mkdir(parents=True, exist_ok=True)
            ss_path = _DEBUG_SCREENSHOT / f"{stem}.png"
            await page.screenshot(path=str(ss_path), full_page=False)
            paths["screenshot"] = str(ss_path.resolve())
            try:
                shutil.copyfile(ss_path, _DEBUG_SCREENSHOT / "naukri_captcha.png")
            except Exception:
                pass
        except Exception as exc:
            logger.debug("captcha_screenshot_failed", error=str(exc))
        try:
            _DEBUG_HTML.mkdir(parents=True, exist_ok=True)
            html_path = _DEBUG_HTML / f"{stem}.html"
            html_path.write_text(await page.content(), encoding="utf-8")
            paths["html"] = str(html_path.resolve())
            try:
                shutil.copyfile(html_path, _DEBUG_HTML / "naukri_captcha.html")
            except Exception:
                pass
        except Exception as exc:
            logger.debug("captcha_html_failed", error=str(exc))
        event = "naukri_access_denied" if reason.lower().startswith("access denied") else "naukri_captcha_detected"
        logger.warning(event, page_number=page_num, job_id=job_id, reason=reason,
                       url=getattr(page, "url", ""), **paths)
        return paths

    # ── Overlay dismissal ──────────────────────────────────────────────────────

    async def _dismiss_overlays(self, page: Page) -> None:
        try:
            await page.keyboard.press("Escape")
            await _delay(page, 300, 500)
        except Exception:
            pass
        for group in (_COOKIE_SELECTORS, _MODAL_DISMISS_SELECTORS):
            for sel in group:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=300):
                        await el.click()
                        await _delay(page, 200, 350)
                        break
                except Exception:
                    continue

    # ── Screenshot helper ──────────────────────────────────────────────────────

    async def _screenshot(self, page: Page, label: str) -> None:
        try:
            _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            path = _DEBUG_DIR / f"{label}_{ts}.png"
            await page.screenshot(path=str(path), full_page=False)
            logger.info("naukri_screenshot_saved", path=str(path))
        except Exception as exc:
            logger.debug("naukri_screenshot_failed", error=str(exc))

    # ── Back-compat ────────────────────────────────────────────────────────────

    @staticmethod
    def _build_search_url(f: FiltersConfig, page_no: int = 1) -> str:
        return build_search_url(f, page_no)


class _PageBlocked(RuntimeError):
    """The page is a CAPTCHA / bot wall / gated redirect, not content.

    ``tab`` — the challenged tab, left open for a human wait (see
    NaukriAgent._fetch_detail); None when it was already closed."""

    def __init__(self, reason: str, tab: Any | None = None) -> None:
        super().__init__(reason)
        self.tab = tab


_RESUME_MESSAGES = {
    "details": "Naukri: resuming detail pages",
    "listing": "Naukri: resuming results pages",
}


async def _status(cb: StatusCallback | None, msg: str) -> None:
    if cb is None:
        return
    try:
        await cb(msg)
    except Exception as exc:
        logger.debug("naukri_status_callback_failed", error=str(exc))
