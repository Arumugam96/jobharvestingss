"""
LinkedIn Vendor-Opportunity Lead Agent: turns LinkedIn posts into vendor leads
for SightSpectrum, an IT staffing / recruitment vendor.

What this agent looks for
─────────────────────────
Posts where a company (or a prime vendor) is looking for EXTERNAL staffing /
recruitment / IT-services vendors:
  • vendor_registration: vendor empanelment, registration or partner onboarding
    ("we are empanelling recruitment agencies", "vendors may register here").
  • vendor_requirement: a contract / C2C / C2H / third-party requirement shared
    for vendors or partners to supply candidates.
Companies hiring employees directly, staffing firms advertising their own bench
or hotlists, job seekers, commentary and ads for unrelated products are dropped
(the reason is recorded in feed_diag.json).

Where it reads posts (the `mode` argument)
──────────────────────────────────────────
  • "feed":   the authenticated Home Feed (/feed/), scrolled.
  • "search": LinkedIn post search (/search/results/content/) for each configured
              vendor phrase, sorted by latest. The Home Feed is ranked and changes
              on every load, so vendor posts are rare there; search finds them.
  • "both":   feed first, then search (default when search is enabled).
Posts are de-duplicated across all of these by their activity URN.

How it reads a page (mirrors what a person or a browsing assistant does)
────────────────────────────────────────────────────────────────────────
  • Finds each post by LinkedIn's "Feed post" accessibility heading, falling back
    to activity-URN containers, then to large list items. Hashed CSS class names
    are only used as optional hints.
  • Clicks every "…more" so the full post text is read.
  • Records the author (from the first profile/company link, skipping "X likes
    this" lines), the post's outbound links (registration forms, lnkd.in) and
    whether the post is promoted. Promoted posts are kept: a company paying to
    promote "vendor registration open" is a lead.
  • If no post element matches at all, splits the page's text on the "Feed post"
    headings so a LinkedIn layout change can't silently yield 0 posts.

Pipeline: keyword pre-filter → LLM classify → LLM extract → validate + score.
A failed LLM call is retried; the run stops only after several consecutive
failures (one malformed reply no longer ends the run).

Security / integrity contract (identical to the other LinkedIn agents)
──────────────────────────────────────────────────────────────────────
• Reuses the authenticated session (session file / persistent Chrome profile).
• NO login automation. Auth is verified by the `li_at` cookie. When it is absent
  on a headed run, the browser waits on LinkedIn's login page for up to 5 minutes
  (a "login needed" banner opens the Live Browser) so a person can log in; the
  session is then saved and the run continues. Headless runs, or a wait that
  times out, raise FeedAuthError and the caller points at
  POST /linkedin-setup-session.
• The LLM NEVER fabricates contact/company info: email, phone and registration
  link are kept only when they appear verbatim in the post (or its links).
• Only publicly-visible post content is stored.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import quote, urlencode

import structlog

from app.config import Settings, get_settings
from app.core.contact_grounding import ground_email, ground_phone
from app.core.contact_normalize import normalize_email, normalize_phone
from app.core.dependencies import get_llm_service
from app.core.exceptions import LLMUnavailableError
from app.models.harvest_run import LlmCallType
from app.services import run_guard
from app.services.feed_tenant_classifier import normalize_market
from app.agents.linkedin_agent import (
    _clean,
    _infer_work_mode,
    _resolve_posted_date,
    _wait_for_page_text_stable,
)
from app.agents.linkedin_lead_agent import _clean_mailto
from app.agents.prospect_intelligence_agent import _HIRING_DOMAIN_MAP, _classify_hiring_domain

if TYPE_CHECKING:
    from playwright.async_api import Page

    from app.services.llm_service import LLMService

logger = structlog.get_logger(__name__)

FEED_URL = "https://www.linkedin.com/feed/"
SEARCH_URL = "https://www.linkedin.com/search/results/content/"
_GATED_PATHS = ("/login", "/checkpoint", "/challenge", "/authwall", "/uas/")
LOGIN_URL = "https://www.linkedin.com/login"

# Manual-login wait: when the session is missing/expired on a headed run, keep the
# browser on LinkedIn's login page this long (polling every _LOGIN_POLL_S) so a
# person can log in through the Live Browser; the run then continues by itself.
_LOGIN_WAIT_S = 300
_LOGIN_POLL_S = 2

# Run modes accepted by harvest(): where posts are read from.
MODE_FEED, MODE_SEARCH, MODE_BOTH = "feed", "search", "both"
MODES = (MODE_FEED, MODE_SEARCH, MODE_BOTH)
_DATE_POSTED_VALUES = ("past-24h", "past-week", "past-month")

# Batch size for the incremental-persistence callback: flush finalized leads to
# the DB every N so the frontend's live "leads saved" counter ticks steadily and
# a mid-run crash keeps what was already extracted.
_PERSIST_BATCH = 5

# Max chars of a single post's text handed to the LLM.
_POST_TEXT_MAX_CHARS = 6_000

# LLM resilience: retry a failed call this many times (with backoff); stop the
# whole run only after this many consecutive failures.
_LLM_POST_RETRIES = 1
_LLM_RETRY_BACKOFF_S = 5.0
_LLM_FAILURE_STREAK_ABORT = 3

# Categories the classifier may assign. Only the keeper categories become leads;
# the rest name why a post was dropped, for the audit trail.
_FEED_CATEGORIES = (
    "vendor_registration", "vendor_requirement",
    "direct_hiring", "vendor_marketing", "job_seeker", "hiring_trends",
    "ad_promo", "company_promo", "repost_no_info", "non_staffing_vendor",
    "irrelevant", "other",
)
_KEEPER_CATEGORIES = ("vendor_registration", "vendor_requirement")

# requester_type (LLM) → scraped_jobs.hiring_entity (existing vocabulary).
_HIRING_ENTITY_MAP = {"end_client": "Direct Client", "staffing_firm": "Staffing Firm"}


def _term_re(terms: set[str] | tuple[str, ...]) -> re.Pattern[str]:
    """Case-insensitive alternation of `terms`, matched on word boundaries so short
    tokens (e.g. "ai", "msp", "rfp") can't match inside longer words."""
    return re.compile(
        r"(?<![a-z0-9])(?:"
        + "|".join(sorted((re.escape(t) for t in terms if t), key=len, reverse=True))
        + r")(?![a-z0-9])",
        re.IGNORECASE,
    )


# Vendor-intent phrases strong enough to make a post a candidate on their own.
_VENDOR_STRONG_TERMS = (
    "vendor", "vendors", "vendor partner", "vendor partners", "vendor onboarding",
    "vendor registration", "empanel", "empanelment", "empanelled", "empaneled",
    "empanelling", "c2c", "corp to corp", "corp-to-corp", "c2h", "contract to hire",
    "contract-to-hire", "third party", "third-party", "3rd party",
    "staffing partner", "staffing partners", "recruitment partner", "recruitment partners",
    "hiring partner", "hiring partners", "talent partner", "talent partners",
    "implementation partner", "implementation partners", "delivery partner",
    "delivery partners", "prime vendor", "subcontract", "sub-contract", "subcontracting",
    "sub vendor", "sub-vendor", "subvendor", "supplier registration", "supplier onboarding",
    "staffing agency", "staffing agencies", "recruitment agency", "recruitment agencies",
    "placement agency", "placement agencies", "recruitment firms", "staffing firms",
    "staffing companies", "consultancies", "staff augmentation", "outsourcing partner",
    "rfp", "rfq", "rfi", "msp",
)
# Weaker phrases: a candidate only together with an IT term.
_VENDOR_WEAK_TERMS = (
    "contract", "contractual", "freelance", "requirement", "requirements",
    "share profiles", "share resumes", "share your profiles", "send profiles",
    "submit profiles", "profiles", "partner", "partners", "partnership", "agency",
    "agencies", "consultants", "consulting firms", "bench", "hotlist",
)
_VENDOR_STRONG_RE = _term_re(_VENDOR_STRONG_TERMS)
_VENDOR_WEAK_RE = _term_re(_VENDOR_WEAK_TERMS)

# IT / technology terminology, flattened from the hiring-domain keyword map plus
# generic role words. Word-boundary matched (see _term_re).
_IT_TERMS: set[str] = {
    kw.strip() for kws, _domain in _HIRING_DOMAIN_MAP for kw in kws if kw.strip()
} | {
    "developer", "developers", "engineer", "engineers", "software", "programming",
    "programmer", "coding", "full stack", "fullstack", "backend", "back-end", "frontend",
    "front-end", "web developer", "data engineer", "data scientist", "devops", "sdet",
    "tester", "python", "java", "javascript", "typescript", "react", "angular", "node",
    ".net", "c#", "golang", "kubernetes", "aws", "azure", "gcp", "sql", "sap", "salesforce",
    "servicenow", "oracle", "machine learning", "ai/ml", "ml engineer", "cloud",
    "cybersecurity", "it", "it services", "it staffing", "it recruitment", "tech hiring",
    "software engineer", "sre", "qa engineer", "technology", "tech",
}
_IT_TERM_RE = _term_re(_IT_TERMS)

StatusCallback = Callable[[str], Awaitable[None]]
BatchCallback = Callable[[list["FeedLead"]], Awaitable[None]]


# ══════════════════════════════════════════════════════════════════════════════
# Types
# ══════════════════════════════════════════════════════════════════════════════

class FeedAuthError(RuntimeError):
    """Raised when LinkedIn can't be reached as an authenticated user (no li_at
    cookie). The caller surfaces an action_required hint pointing at
    POST /linkedin-setup-session."""


class FeedBrowserClosedError(RuntimeError):
    """Raised when the browser / tab / context went away mid-run (window closed in
    the Live Browser, Chrome crash, OOM). NOT an auth problem: posts already
    collected are still processed and saved."""


_BROWSER_CLOSED_MARKERS = (
    "target page, context or browser has been closed",
    "target closed",
    "browser has been closed",
    "context has been closed",
    "page has been closed",
    "browser has disconnected",
    "connection closed",
)


def _is_browser_closed_error(exc: BaseException) -> bool:
    """True when a Playwright error means the page/context/browser is gone."""
    if isinstance(exc, FeedBrowserClosedError):
        return True
    msg = str(exc).lower()
    return any(m in msg for m in _BROWSER_CLOSED_MARKERS)


def _safe_url(page: Any) -> str:
    """page.url for logging; never raises (the page may already be closed)."""
    try:
        return str(page.url)[:120]
    except Exception:
        return "?"


def _page_closed(page: Any) -> bool | None:
    """page.is_closed() for logging; None when it can't be determined."""
    try:
        return bool(page.is_closed())
    except Exception:
        return None


@dataclass
class FeedLead:
    """One validated vendor-opportunity lead extracted from a LinkedIn post."""
    # ── Opportunity ───────────────────────────────────────────────────────────
    job_title:        str = ""             # roles needed, or a generic opportunity label
    company:          str = ""             # company that needs vendors / candidates
    location:         str = ""
    salary:           str = ""             # rate / budget, only if stated
    employment_type:  str = ""             # engagement model: C2C, C2H, Contract, Empanelment…
    work_mode:        str = "not_specified"
    skills:           list[str] = field(default_factory=list)
    description:      str = ""
    posted_date:      str = ""             # YYYY-MM-DD (resolved in Python) or ""
    domain:           str = "IT"           # IT sub-domain (display only)
    opportunity_type: str = ""             # vendor_registration | vendor_requirement
    requester_type:   str = "unknown"      # end_client | staffing_firm | unknown
    how_to_engage:    str = ""
    registration_link: str = ""            # only if verbatim in the post / its links
    # ── Poster info ───────────────────────────────────────────────────────────
    recruiter_name:        str = ""
    recruiter_title:       str = ""
    recruiter_company:     str = ""
    recruiter_profile_url: str = ""
    recruiter_email:       str = ""        # only if verbatim in the post
    recruiter_phone:       str = ""        # only if verbatim in the post
    # ── Provenance / meta ─────────────────────────────────────────────────────
    post_url:        str = ""              # post permalink (from URN)
    post_urn:        str = ""
    is_repost:       bool = False
    is_promoted:     bool = False
    found_via:       list[str] = field(default_factory=list)  # "feed" / "search: <query>"
    category:        str = ""
    lead_confidence: float = 0.0
    # Client market judged by the extraction LLM ("US" | "IN" | "unknown"); used only
    # as the LLM step of app/services/feed_tenant_classifier.py (phone/location win).
    market:          str = "unknown"

    def to_scraped_job_dict(self) -> dict[str, Any]:
        """Map onto ScrapedJobORM's canonical dict shape (see
        HarvestRunService.bulk_insert_scraped_jobs). source="LinkedIn Feed"
        distinguishes these rows from LinkedIn Jobs / Naukri / Dice. String fields
        are capped at their column sizes so a long LLM value can't fail the insert."""
        return {
            "source":                 "LinkedIn Feed",
            "job_title":              self.job_title[:500],
            "company":                self.company[:500],
            "location":               self.location[:500],
            "salary":                 self.salary,
            "experience":             "",
            "posted_date":            self.posted_date[:100],
            "job_url":                self.post_url,
            "job_description":        self.description,
            "job_description_html":   "",
            "skills":                 self.skills,
            "work_mode":              self.work_mode,
            "company_url":            "",
            "employment_type":        self.employment_type[:100],
            "job_type":               self.opportunity_type,
            "domain":                 (self.domain or "IT")[:50],
            "hiring_entity":          _HIRING_ENTITY_MAP.get(self.requester_type, "Ambiguous"),
            "job_poster_name":        self.recruiter_name or None,
            "job_poster_designation": self.recruiter_title or None,
            "linkedin_profile_url":   self.recruiter_profile_url or None,
            "current_company":        self.recruiter_company or self.company or None,
            "email_id":               self.recruiter_email or None,
            "contact_number":         self.recruiter_phone or None,
            "lead_confidence":        self.lead_confidence,
        }


# ══════════════════════════════════════════════════════════════════════════════
# Candidate filter (deterministic, no LLM)
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# Posting date (deterministic, no LLM)
# ══════════════════════════════════════════════════════════════════════════════

_URN_ID_RE = re.compile(r"urn:li:(?:activity|ugcPost|share):(\d+)")
_URN_EPOCH_FLOOR = datetime(2010, 1, 1, tzinfo=timezone.utc)

# Feed posts show their age as a bare short form with no "ago" ("3h •", "2d •",
# "1w •", "3mo •", "1yr •"), which linkedin_agent._resolve_posted_date (built for
# the Jobs page's "3 days ago") never matches. On the feed a bare "m" = minutes.
_FEED_AGE_RE = re.compile(
    r"(?<![\w])(?P<num>\d+)\s*(?P<unit>years?|yrs?|y|months?|mos?|weeks?|w|days?|d|"
    r"hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
    re.IGNORECASE,
)


def _date_from_urn(urn: str, harvest_date: datetime) -> str | None:
    """Exact posting date (UTC, YYYY-MM-DD) decoded from a LinkedIn post URN.

    LinkedIn post IDs are snowflake-style: the top 41 bits of the 64-bit ID are
    the creation time in Unix milliseconds (id >> 22). Returns None when the URN
    is missing or the decoded time is implausible (before 2010 / in the future)."""
    m = _URN_ID_RE.search(urn or "")
    if not m:
        return None
    try:
        created = datetime.fromtimestamp((int(m.group(1)) >> 22) / 1000, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    if created < _URN_EPOCH_FLOOR or created > harvest_date + timedelta(days=1):
        return None
    return created.strftime("%Y-%m-%d")


def _date_from_feed_age(timestamp: str, harvest_date: datetime) -> str | None:
    """Convert a feed age label ("3h", "2d •", "1w • Edited", "3mo") to YYYY-MM-DD."""
    m = _FEED_AGE_RE.search(timestamp or "")
    if not m:
        return None
    num, unit = int(m.group("num")), m.group("unit").lower()
    if unit.startswith("y"):
        days = num * 365
    elif unit.startswith("mo"):
        days = num * 30
    elif unit.startswith("w"):
        days = num * 7
    elif unit.startswith("d"):
        days = num
    else:  # hours / minutes / seconds → today
        days = 0
    return (harvest_date - timedelta(days=days)).strftime("%Y-%m-%d")


def _resolve_feed_posted_date(raw: dict, text: str, harvest_date: datetime) -> str:
    """Best available posting date for a feed post; never empty, so the date
    filter (which compares YYYY-MM-DD strings) can always see the row.

    Priority: URN-encoded creation time (exact) → the post's age label → a
    relative/absolute phrase in the post text → the harvest date (when seen)."""
    timestamp = _clean(raw.get("timestamp", ""))
    return (
        _date_from_urn(str(raw.get("urn") or ""), harvest_date)
        or _date_from_feed_age(timestamp, harvest_date)
        or _resolve_posted_date(timestamp, harvest_date)
        or _resolve_posted_date(text, harvest_date)
        or harvest_date.strftime("%Y-%m-%d")
    )


def _is_candidate(text: str) -> bool:
    """Cheap first-stage gate: keep a post if it uses a strong vendor phrase, or a
    weaker one (contract, requirement, share profiles…) together with an IT term.
    Broad recall: this only removes obviously-irrelevant posts to cut LLM cost;
    the LLM makes the actual keep/drop decision."""
    raw = text or ""
    if len(raw) < 12:
        return False
    if _VENDOR_STRONG_RE.search(raw):
        return True
    return bool(_VENDOR_WEAK_RE.search(raw) and _IT_TERM_RE.search(raw))


def _search_url(query: str, date_posted: str = "") -> str:
    """LinkedIn post-search URL for `query`, newest first, optionally limited to a
    date window (past-24h / past-week / past-month; anything else = any time)."""
    params = {"keywords": query, "origin": "FACETED_SEARCH", "sortBy": '"date_posted"'}
    if date_posted in _DATE_POSTED_VALUES:
        params["datePosted"] = f'"{date_posted}"'
    return f"{SEARCH_URL}?{urlencode(params, quote_via=quote)}"


def _url_key(url: str) -> str:
    """Comparison form of a URL: no scheme, no www., no trailing slash, lowercase."""
    u = re.sub(r"^https?://", "", (url or "").strip().lower())
    return re.sub(r"^www\.", "", u).rstrip("/")


def _ground_link(link: str | None, links: list[str], text: str) -> str:
    """Keep an LLM-returned link only if it is one of the post's own links or is
    written in the post text. Returns the grounded URL or ""."""
    link = (link or "").strip()
    if not link or not re.match(r"^(https?://)?[\w.-]+\.[a-z]{2,}(/\S*)?$", link, re.I):
        return ""
    key = _url_key(link)
    for candidate in links:
        if _url_key(candidate) == key:
            return candidate
    if key and key in (text or "").lower():
        return link if link.lower().startswith("http") else f"https://{link}"
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# In-browser post collection
# ══════════════════════════════════════════════════════════════════════════════

# Click every "…more" / "see more" toggle inside <main> so the full post text is in
# the DOM before it is read. Comment / result "show more" buttons are skipped.
_EXPAND_JS = r"""() => {
    let n = 0;
    const root = document.querySelector('main') || document.body;
    for (const b of root.querySelectorAll('button, [role="button"]')) {
        const t = ((b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '')).trim().toLowerCase();
        if (!(/(…|\.\.\.)\s*(see )?more\b/.test(t) || /^see more\b/.test(t))) continue;
        if (/comment|repl|result|jobs/.test(t)) continue;
        if (b.getAttribute('aria-expanded') === 'true') continue;
        try { b.click(); n++; } catch (e) { /* ignore */ }
        if (n >= 40) break;
    }
    return n;
}"""

# Enumerate post containers, most-stable signal first:
#   1. LinkedIn's accessibility heading "Feed post" that every post carries
#      (present on the Home Feed in both the old and the hashed-class DOM);
#   2. containers carrying the activity URN (old DOM, search results);
#   3. large list items inside <main> (last resort, e.g. search-result cards).
# For each post we return raw fields; Python does dedup + parsing.
_COLLECT_POSTS_JS = r"""() => {
    const SEL = 'div.feed-shared-update-v2, '
        + 'div[data-urn*="urn:li:activity"], div[data-id*="urn:li:activity"], '
        + 'div[data-urn*="urn:li:ugcPost"], div[data-id*="urn:li:ugcPost"], '
        + 'div[data-urn*="urn:li:share"], div[data-id*="urn:li:share"]';
    const URN_RE = /urn:li:(?:activity|ugcPost|share):\d+/;
    const URN_ENC_RE = /urn%3Ali%3A(?:activity|ugcPost|share)%3A\d+/;
    const SOCIAL_RE = /\b(likes|loves|celebrates|supports|finds|commented|reposted|follows|followed by|reacted)\b/i;

    function pickText(root, sels) {
        for (const s of sels) {
            const el = root.querySelector(s);
            if (el && el.textContent && el.textContent.trim()) return el.textContent.trim();
        }
        return '';
    }
    function outermost(arr) {
        return arr.filter(el => !arr.some(o => o !== el && o.contains(el)));
    }

    let strategy = 'heading';
    let found = [];
    for (const h of document.querySelectorAll('h2, h3')) {
        if (!/^feed post\b/i.test((h.textContent || '').trim())) continue;
        const c = h.closest(SEL) || h.closest('[role="listitem"], article, [role="article"]') || h.parentElement;
        if (c && !found.includes(c)) found.push(c);
    }
    if (!found.length) {
        strategy = 'urn';
        found = outermost(Array.from(document.querySelectorAll(SEL)));
    }
    if (!found.length) {
        strategy = 'listitem';
        found = outermost(Array.from(document.querySelectorAll(
            'main [role="listitem"], main li, main article'
        )).filter(el => (el.innerText || '').trim().length >= 150));
    }

    const out = [];
    for (const el of found) {
        try {
            const fullText = (el.innerText || el.textContent || '').trim();
            if (fullText.length < 40) continue;

            // Activity URN: own attribute → descendant → ancestor → anywhere in the HTML.
            let urn = el.getAttribute('data-urn') || el.getAttribute('data-id') || '';
            if (!URN_RE.test(urn)) {
                const inner = el.querySelector('[data-urn*="urn:li:"], [data-id*="urn:li:"]');
                urn = inner ? (inner.getAttribute('data-urn') || inner.getAttribute('data-id') || '') : '';
            }
            if (!URN_RE.test(urn) && el.parentElement) {
                const anc = el.parentElement.closest('[data-urn*="urn:li:"], [data-id*="urn:li:"]');
                if (anc) urn = anc.getAttribute('data-urn') || anc.getAttribute('data-id') || '';
            }
            let m = URN_RE.exec(urn);
            if (!m) {
                const html = (el.outerHTML || '').slice(0, 300000);
                m = URN_RE.exec(html) || URN_ENC_RE.exec(html);
            }
            try { urn = m ? decodeURIComponent(m[0]) : ''; } catch (e) { urn = m ? m[0] : ''; }

            // Author: actor selectors (old DOM), else the first named profile/company
            // link that isn't a "X likes this" social-proof line.
            let authorName = pickText(el, [
                '.update-components-actor__title span[aria-hidden="true"]',
                '.update-components-actor__name span[aria-hidden="true"]',
                '.update-components-actor__name',
                '.update-components-actor__title',
            ]);
            let profileUrl = '';
            const actorLink = el.querySelector('a.update-components-actor__meta-link');
            if (actorLink) profileUrl = actorLink.href || '';
            if (!authorName || !profileUrl) {
                for (const a of el.querySelectorAll('a[href*="/in/"], a[href*="/company/"]')) {
                    const name = (a.innerText || '').trim().split('\n')[0].trim();
                    if (!name || name.length > 80) continue;
                    const ctx = ((a.parentElement && a.parentElement.innerText) || '').slice(0, 200);
                    if (SOCIAL_RE.test(ctx) && !ctx.includes('•')) continue;
                    if (!authorName) authorName = name;
                    if (!profileUrl) profileUrl = a.href || '';
                    break;
                }
            }

            const headline = pickText(el, [
                '.update-components-actor__description',
                '.update-components-actor__subtitle',
            ]);

            let timestamp = pickText(el, [
                '.update-components-actor__sub-description span[aria-hidden="true"]',
                '.update-components-actor__sub-description',
                'time',
            ]);
            if (!timestamp) {
                const tm = /(?:^|\s)(\d+\s?(?:s|m|h|d|w|mo|yr|y))\s*•/.exec(fullText.slice(0, 600));
                if (tm) timestamp = tm[1];
            }

            let text = pickText(el, [
                '.update-components-text',
                '.feed-shared-update-v2__description',
                '.feed-shared-inline-show-more-text',
                '[data-test-id="main-feed-activity-card__commentary"]',
                'span.break-words',
            ]);
            if (!text || text.length < 20) text = fullText;

            const head = fullText.slice(0, 400);
            const header = el.querySelector('.update-components-header, .update-components-actor__supplementary-actor-info');
            const headerTxt = header ? (header.textContent || '').toLowerCase() : '';
            const isRepost = headerTxt.includes('repost') || headerTxt.includes('reshare') || /reposted this/i.test(head);
            const isPromoted = /\bPromoted\b/.test(head);

            // Outbound links (registration forms, lnkd.in…). LinkedIn's /redir/
            // wrapper is unwrapped; other linkedin.com links are skipped except forms/events.
            const links = [];
            for (const a of el.querySelectorAll('a[href]')) {
                let h = a.href || '';
                if (!/^https?:/i.test(h)) continue;
                let u;
                try { u = new URL(h); } catch (e) { continue; }
                const host = u.hostname.toLowerCase();
                const isLi = host === 'linkedin.com' || host.endsWith('.linkedin.com');
                if (isLi && u.pathname.startsWith('/redir/')) {
                    const target = u.searchParams.get('url');
                    if (!target) continue;
                    h = target;
                } else if (isLi && !/^\/(forms|events)\//.test(u.pathname)) {
                    continue;
                }
                if (!links.includes(h)) links.push(h);
                if (links.length >= 10) break;
            }

            const mailto = el.querySelector('a[href^="mailto:"]');
            var mailtoEmail = mailto
                ? (mailto.href || '').replace(/^mailto:/i, '').split('?')[0].trim() : '';
            try { mailtoEmail = decodeURIComponent(mailtoEmail).trim(); } catch (e) { /* raw */ }

            out.push({
                urn: urn,
                author_name: authorName,
                author_profile_url: profileUrl,
                author_headline: headline,
                timestamp: timestamp,
                text: text,
                full_text: fullText.slice(0, 8000),
                is_repost: isRepost,
                is_promoted: isPromoted,
                links: links,
                mailto: mailtoEmail,
            });
        } catch (e) { /* skip malformed post */ }
    }
    return { count: found.length, strategy: strategy, posts: out };
}"""

# Last-resort reader: split <main>'s visible text on the "Feed post" headings.
# Used only when no post element matched at all, so a layout change degrades to
# plain-text posts (no URN / author link) instead of 0 posts.
_PAGE_TEXT_JS = r"""() => {
    const main = document.querySelector('main') || document.body;
    const txt = (main.innerText || '');
    const parts = txt.split(/\n\s*Feed post(?: number \d+)?\s*\n/i).slice(1);
    return parts.map(p => p.trim()).filter(p => p.length >= 60).slice(0, 60);
}"""

# Scroll nudges + "Show more results" for search pages. Returns nothing.
_SCROLL_JS = r"""() => {
    const scroller = document.scrollingElement || document.documentElement;
    scroller.scrollTo(0, scroller.scrollHeight);
    const posts = document.querySelectorAll(
        'div.feed-shared-update-v2, div[data-urn*="urn:li:activity"], div[data-id*="urn:li:activity"], main [role="listitem"], main li'
    );
    if (posts.length) posts[posts.length - 1].scrollIntoView({block: 'end'});
    window.dispatchEvent(new Event('scroll'));
    for (const b of document.querySelectorAll('main button')) {
        if (/^show more results$/i.test((b.innerText || '').trim())) { try { b.click(); } catch (e) {} break; }
    }
}"""


# ══════════════════════════════════════════════════════════════════════════════
# Agent
# ══════════════════════════════════════════════════════════════════════════════

class LinkedInFeedAgent:
    """
    Harvests vendor-opportunity leads from LinkedIn (Home Feed and/or post search).

    One instance per run. Owns an LLMService whose token usage + per-call audit
    log the caller persists to llm_calls.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        llm_service: "LLMService | None" = None,
    ) -> None:
        """Read the scroll/stop limits, search settings, LLM call cap and confidence
        gate from settings. The harvest start time is fixed here so every relative
        post date in the run resolves against the same reference point."""
        self._settings = settings or get_settings()
        self._llm_service = llm_service or get_llm_service(self._settings)
        self._llm_calls_made = 0
        self._llm_failure_streak = 0
        self._harvest_started_at = datetime.now(timezone.utc)

        s = self._settings
        self._max_posts        = int(s.linkedin_feed_max_posts)
        self._max_scrolls      = int(s.linkedin_feed_max_scrolls)
        self._min_scrolls      = int(s.linkedin_feed_min_scrolls)
        self._scroll_delay_ms  = int(s.linkedin_feed_scroll_delay_ms)
        self._no_new_rounds    = int(s.linkedin_feed_no_new_stop_rounds)
        self._empty_dom_rounds = int(s.linkedin_feed_empty_dom_stop_rounds)
        self._llm_max_calls    = int(s.linkedin_feed_llm_max_calls)
        self._min_confidence   = float(s.linkedin_feed_min_confidence)
        self._search_queries   = list(s.linkedin_feed_search_query_list)
        self._search_date      = str(s.linkedin_feed_search_date_posted or "").strip()
        self._search_scrolls   = int(s.linkedin_feed_search_max_scrolls)
        self._search_max_posts = int(s.linkedin_feed_search_max_posts_per_query)

        # Per-source collection counts ("feed", "search: <q>") for the diagnostic.
        self._source_counts: dict[str, int] = {}

        # Set by harvest(): whether a person can log in through the Live Browser
        # (headed run) and which LinkedIn account's session to save after a login.
        self._headless: bool = True
        self._account:  str  = "1"

        # Set by harvest() when post collection stopped early (browser closed,
        # navigation/auth failure mid-search) but already-collected posts were
        # still processed. "" = collection finished normally. Read by the route.
        self.collection_error: str = ""
        # Why/when the browser went away (page/context close or crash events).
        self._browser_events: list[str] = []

    def default_mode(self) -> str:
        """"both" when post search is enabled in settings, else "feed"."""
        return MODE_BOTH if self._settings.linkedin_feed_search_enabled else MODE_FEED

    def get_token_usage(self) -> dict:
        """LLM token-usage summary for this agent's calls (an empty summary when no LLM service)."""
        if self._llm_service is None:
            from app.services.llm_service import empty_usage_summary
            return empty_usage_summary()
        return self._llm_service.get_usage_summary()

    def get_llm_call_log(self) -> list[dict]:
        """Per-call LLM log from the service ([] when no LLM service is configured)."""
        if self._llm_service is None:
            return []
        return self._llm_service.get_call_log()

    # ── Public entry point ──────────────────────────────────────────────────────

    async def harvest(
        self,
        headless: bool = True,
        slow_mo: int = 0,
        on_status: StatusCallback | None = None,
        on_batch: BatchCallback | None = None,
        mode: str | None = None,
    ) -> list[FeedLead]:
        """Read posts from the Home Feed and/or post search (`mode`), then return
        validated vendor-opportunity leads. Raises FeedAuthError when the session is
        not authenticated, ValueError for an unknown mode."""
        mode = mode or self.default_mode()
        if mode not in MODES:
            raise ValueError(f"Unknown LinkedIn feed mode {mode!r}; expected one of {MODES}")

        from app.scrapers.browser_manager import BrowserManager, PersistentBrowserManager
        from app.services.config_service import ConfigService
        from app.services.session_manager import SessionManager, account_profile_dir

        _cfg           = ConfigService().load()
        chrome_profile = _cfg.browser.chrome_profile
        account        = _cfg.sources.linkedin_account
        self._headless = headless
        self._account  = str(account or "1")
        sm = SessionManager("linkedin", account)
        storage_state_arg = sm.storage_state_arg()

        if storage_state_arg:
            logger.debug("linkedin_feed_using_session_file", session_file=storage_state_arg, account=account)
            browser_ctx = BrowserManager(
                headless=headless, slow_mo=slow_mo, storage_state=storage_state_arg,
            )
        else:
            account_profile = account_profile_dir(chrome_profile, account)
            logger.warning(
                "linkedin_feed_no_session_file",
                account=account,
                hint=f"No session file at {sm.session_path} — falling back to Chrome profile "
                     f"{account_profile}. Run POST /linkedin-setup-session to create it.",
            )
            browser_ctx = PersistentBrowserManager(
                profile_dir=account_profile, headless=headless, slow_mo=slow_mo,
            )

        logger.info(
            "linkedin_feed_harvest_start",
            mode=mode, headless=headless, account=self._account,
            session=("session_file" if storage_state_arg else "chrome_profile"),
            search_queries=self._search_queries if mode != MODE_FEED else [],
            search_date=self._search_date or "any",
            max_posts=self._max_posts, max_scrolls=self._max_scrolls,
            search_max_scrolls=self._search_scrolls,
            search_max_posts_per_query=self._search_max_posts,
            llm_max_calls=self._llm_max_calls, min_confidence=self._min_confidence,
            has_on_batch=on_batch is not None,
        )

        # ── Phase 1: collect posts (needs the browser) ─────────────────────────
        # Posts are only held in memory until Phase 2, so a failure here used to
        # discard everything collected so far. Now an interruption after some posts
        # were collected is logged and Phase 2 still runs on what we have.
        collected: dict[str, dict] = {}
        try:
            async with browser_ctx as bm:
                page = await bm.new_page()
                self._watch_browser(page)
                if mode in (MODE_FEED, MODE_BOTH):
                    await self._open(page, FEED_URL, on_status=on_status)
                    await self._collect_from_page(
                        page, collected, label="feed", max_new=self._max_posts,
                        max_scrolls=self._max_scrolls, min_scrolls=self._min_scrolls,
                        on_status=on_status,
                    )
                if mode in (MODE_SEARCH, MODE_BOTH):
                    await self._run_searches(page, collected, on_status=on_status)
        except Exception as exc:
            logger.warning(
                "linkedin_feed_collection_interrupted",
                error_type=type(exc).__name__, error=str(exc)[:500],
                posts_collected=len(collected), sources=dict(self._source_counts),
                browser_events=self._browser_events,
                will_process_collected=bool(collected),
            )
            if not collected:
                if isinstance(exc, FeedAuthError) or not _is_browser_closed_error(exc):
                    raise
                raise FeedBrowserClosedError(
                    f"Browser closed before any posts were collected: {exc}"
                ) from exc
            self.collection_error = f"{type(exc).__name__}: {str(exc)[:300]}"

        raw_posts = list(collected.values())
        logger.info(
            "linkedin_feed_collection_done",
            posts_collected=len(raw_posts), sources=dict(self._source_counts),
            interrupted=bool(self.collection_error),
        )
        if on_status and self.collection_error:
            await on_status(
                f"Browser stopped early — processing the {len(raw_posts)} posts already collected…"
            )

        # ── Phase 2: classify + extract + persist (no browser needed) ──────────
        leads = await self._process_posts(raw_posts, on_status=on_status, on_batch=on_batch)

        logger.info(
            "linkedin_feed_harvest_completed",
            mode=mode, posts_collected=len(raw_posts), leads=len(leads),
            llm_calls=self._llm_calls_made, sources=self._source_counts,
            collection_error=self.collection_error or None,
        )
        return leads

    def _watch_browser(self, page: "Page") -> None:
        """Log page/context close + crash events so a "browser has been closed"
        error can be traced to its cause (window closed in the Live Browser, renderer
        crash, context shut down…)."""
        def _record(event: str) -> None:
            stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
            self._browser_events.append(f"{stamp} {event}")
            logger.warning("linkedin_feed_browser_event", event=event, url=_safe_url(page))

        try:
            page.on("close", lambda *_: _record("page_closed"))
            page.on("crash", lambda *_: _record("page_crashed"))
            page.context.on("close", lambda *_: _record("context_closed"))
            page.context.on("page", lambda p: logger.info(
                "linkedin_feed_browser_new_tab", url=_safe_url(p),
            ))
            browser = page.context.browser
            if browser is not None:
                browser.on("disconnected", lambda *_: _record("browser_disconnected"))
        except Exception as exc:
            logger.debug("linkedin_feed_browser_watch_failed", error=str(exc))

    # ── Open + auth-gate a page ─────────────────────────────────────────────────

    async def _open(self, page: "Page", url: str, on_status: StatusCallback | None = None) -> None:
        """Navigate to `url`, dismiss overlays and verify the session is logged in.

        When the session is missing/expired on a headed run, waits up to
        _LOGIN_WAIT_S for a person to log in through the Live Browser, saves the new
        session and reopens `url`. Raises FeedAuthError if navigation fails, or if
        the session is not authenticated and can't be (headless run, or the login
        wait timed out / was stopped)."""
        await self._goto(page, url)

        if await self._is_authenticated(page):
            logger.info("linkedin_feed_page_ready", url=page.url[:120])
            return

        logger.warning("linkedin_feed_not_authenticated", url=page.url[:80])
        if self._headless:
            # Nobody can type into a headless browser — fail fast as before.
            self._flag_login_required(
                "LinkedIn session expired — the LinkedIn Feed run stopped", waiting=False,
            )
            raise FeedAuthError(
                "LinkedIn is not authenticated (no li_at cookie). "
                "Run POST /linkedin-setup-session to log in once."
            )

        await self._wait_for_login(page, on_status)

        # Logged in: back to the page this step wanted, and re-verify.
        await self._goto(page, url)
        if not await self._is_authenticated(page):
            logger.warning("linkedin_feed_not_authenticated_after_login", url=page.url[:80])
            raise FeedAuthError(
                "LinkedIn still not authenticated after login (redirected to "
                f"{page.url[:80]}). Run POST /linkedin-setup-session to log in."
            )
        logger.info("linkedin_feed_page_ready", url=page.url[:120], after_login=True)

    async def _goto(self, page: "Page", url: str) -> None:
        """Navigate to `url` and dismiss overlays. Raises FeedBrowserClosedError when
        the browser/tab is gone, FeedAuthError on any other navigation failure."""
        logger.info("linkedin_feed_navigate", url=url[:160], page_closed=_page_closed(page))
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(3_000)
        except Exception as exc:
            closed = _is_browser_closed_error(exc)
            logger.error(
                "linkedin_feed_navigation_failed", url=url[:120], error=str(exc)[:500],
                browser_closed=closed, page_closed=_page_closed(page),
                browser_events=self._browser_events,
            )
            if closed:
                raise FeedBrowserClosedError(
                    f"Browser was closed while opening {url[:80]}: {exc}"
                ) from exc
            raise FeedAuthError(f"Could not open LinkedIn ({url[:80]}): {exc}") from exc
        await self._dismiss_overlays(page)

    @staticmethod
    async def _is_authenticated(page: "Page") -> bool:
        """Logged in = li_at cookie present AND not parked on a login/checkpoint
        page. Auth is confirmed by the cookie, not URL shape (LinkedIn will show a
        guest shell without redirecting) — same check as the other agents."""
        try:
            cookies = await page.context.cookies()
        except Exception:
            cookies = []
        has_li_at = any(c.get("name") == "li_at" for c in cookies)
        return has_li_at and not any(p in page.url for p in _GATED_PATHS)

    async def _wait_for_login(self, page: "Page", on_status: StatusCallback | None) -> None:
        """Keep this run's (headed) browser on LinkedIn's login page for up to
        _LOGIN_WAIT_S so a person can log in via the Live Browser (MFA/checkpoint
        included). Raises a "login needed" banner whose button opens the Live
        Browser. On success saves the session file (future runs reuse it) and
        clears the banner; raises FeedAuthError on timeout or a stop request."""
        minutes = max(1, _LOGIN_WAIT_S // 60)
        logger.info(
            "linkedin_feed_waiting_for_login", timeout_s=_LOGIN_WAIT_S, account=self._account,
        )
        self._flag_login_required(
            "LinkedIn login needed — the LinkedIn Feed run is waiting in the Live Browser",
            waiting=True,
        )
        if on_status:
            await on_status(
                f"LinkedIn login needed — open the Live Browser and log in "
                f"(waiting up to {minutes} minutes)…"
            )

        # Put the login form on screen unless LinkedIn already redirected there
        # (a logged-out "guest shell" feed has no form to type into).
        if not any(p in page.url for p in _GATED_PATHS):
            try:
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=20_000)
            except Exception as exc:
                logger.debug("linkedin_feed_login_page_nav_failed", error=str(exc))

        waited = 0
        while waited < _LOGIN_WAIT_S:
            if run_guard.is_stop_requested():
                logger.info("linkedin_feed_login_wait_stopped_by_user", waited_s=waited)
                self._clear_login_required()
                raise FeedAuthError("Run stopped while waiting for LinkedIn login.")
            await page.wait_for_timeout(_LOGIN_POLL_S * 1_000)
            waited += _LOGIN_POLL_S
            if await self._is_authenticated(page):
                logger.info("linkedin_feed_login_detected", waited_s=waited, account=self._account)
                await self._save_session(page)
                self._clear_login_required()
                if on_status:
                    await on_status("LinkedIn login detected — starting the feed harvest…")
                return

        logger.error("linkedin_feed_login_timeout", timeout_s=_LOGIN_WAIT_S)
        self._flag_login_required(
            f"LinkedIn login not completed within {minutes} minutes — "
            "the LinkedIn Feed run stopped",
            waiting=False,
        )
        raise FeedAuthError(
            f"LinkedIn login not completed within {minutes} minutes. "
            "Start the run again and log in through the Live Browser."
        )

    async def _save_session(self, page: "Page") -> None:
        """Persist the freshly logged-in session to the account's session file so
        later runs (feed and jobs harvest) reuse it. Best-effort."""
        try:
            from app.services.session_manager import SessionManager
            await SessionManager("linkedin", self._account).save_session(page)
        except Exception as exc:
            logger.warning("linkedin_feed_session_save_failed", error=str(exc))

    def _flag_login_required(self, reason: str, *, waiting: bool) -> None:
        """Raise the global "LinkedIn login needed" banner. waiting=True: this run's
        own browser is on the login page (the banner opens the Live Browser, with a
        countdown); False: nobody is waiting — the banner starts the setup flow."""
        try:
            from app.services import login_required as lr
            lr.raise_login_required(
                lr.SOURCE_LINKEDIN, account=self._account, reason=reason,
                waiting_s=_LOGIN_WAIT_S if waiting else None,
                action={"type": lr.ACTION_LIVE_BROWSER} if waiting else None,
            )
            if not waiting:
                lr.set_waiting(
                    lr.SOURCE_LINKEDIN, self._account, None,
                    action=lr.default_action(lr.SOURCE_LINKEDIN, self._account, lr.KIND_LOGIN),
                )
        except Exception as exc:
            logger.debug("linkedin_feed_login_required_flag_failed", error=str(exc))

    def _clear_login_required(self) -> None:
        """Clear this account's pending "LinkedIn login needed" banner (errors only logged)."""
        try:
            from app.services import login_required as lr
            lr.clear(lr.SOURCE_LINKEDIN, self._account, kind=lr.KIND_LOGIN)
        except Exception as exc:
            logger.debug("linkedin_feed_login_required_clear_failed", error=str(exc))

    async def _dismiss_overlays(self, page: "Page") -> None:
        """Close any visible dismiss/close popups (e.g. messaging bubbles); failures are ignored."""
        for sel in (
            "button[aria-label='Dismiss']",
            "button.msg-overlay-bubble-header__control--close",
            "button[aria-label='Close']",
        ):
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=800):
                    await btn.click()
                    await page.wait_for_timeout(300)
            except Exception:
                pass

    # ── Post search ─────────────────────────────────────────────────────────────

    async def _run_searches(
        self, page: "Page", collected: dict[str, dict], on_status: StatusCallback | None = None,
    ) -> None:
        """Run LinkedIn post search for every configured vendor phrase and add the
        results to `collected`. A failing query is logged and skipped; an auth
        failure (FeedAuthError) propagates."""
        queries = self._search_queries
        if not queries:
            logger.warning("linkedin_feed_search_no_queries")
            return
        for i, query in enumerate(queries, start=1):
            if run_guard.is_stop_requested():
                logger.info("linkedin_feed_search_stopped_by_user", query=query)
                break
            logger.info(
                "linkedin_feed_search_query_start",
                query=query, n=i, of=len(queries), collected_so_far=len(collected),
            )
            if on_status:
                await on_status(f"Searching LinkedIn posts: \"{query}\" ({i}/{len(queries)})…")
            try:
                await self._open(page, _search_url(query, self._search_date), on_status=on_status)
            except (FeedAuthError, FeedBrowserClosedError):
                # The browser is gone / not logged in: later queries would fail the
                # same way. harvest() still processes the posts collected so far.
                logger.warning(
                    "linkedin_feed_search_aborted", query=query, n=i, of=len(queries),
                    remaining_queries=queries[i:], collected_so_far=len(collected),
                )
                raise
            except Exception as exc:
                logger.warning("linkedin_feed_search_open_failed", query=query, error=str(exc))
                continue
            new = await self._collect_from_page(
                page, collected, label=f"search: {query}", max_new=self._search_max_posts,
                max_scrolls=self._search_scrolls, min_scrolls=0, on_status=on_status,
            )
            logger.info(
                "linkedin_feed_search_query_done", query=query, new=new, total=len(collected),
            )

    # ── Scroll-collect a page ───────────────────────────────────────────────────

    async def _expand_posts(self, page: "Page") -> None:
        """Click every "…more" toggle so full post text is readable. If a click
        navigated away (some layouts open the post), go back."""
        before = page.url.split("?")[0]
        try:
            clicked = int(await page.evaluate(_EXPAND_JS) or 0)
        except Exception as exc:
            logger.debug("linkedin_feed_expand_failed", error=str(exc))
            return
        if not clicked:
            return
        await page.wait_for_timeout(600)
        if page.url.split("?")[0] != before:
            logger.info("linkedin_feed_expand_navigated_back", to=page.url[:100])
            try:
                await page.go_back(wait_until="domcontentloaded", timeout=15_000)
                await page.wait_for_timeout(1_500)
            except Exception:
                pass

    async def _read_page_posts(self, page: "Page") -> tuple[int, str, list[dict]]:
        """Return (container_count, strategy, posts) for the posts currently rendered.
        Falls back to splitting the page text when no post element matched."""
        try:
            result = await page.evaluate(_COLLECT_POSTS_JS)
        except Exception as exc:
            logger.warning("linkedin_feed_collect_eval_failed", error=str(exc))
            result = {"count": 0, "strategy": "error", "posts": []}
        count = int(result.get("count", 0) or 0)
        posts = list(result.get("posts", []) or [])
        strategy = str(result.get("strategy") or "")
        if count == 0:
            try:
                parts = await page.evaluate(_PAGE_TEXT_JS) or []
            except Exception:
                parts = []
            posts = [{"urn": "", "text": p, "full_text": p, "page_text_fallback": True} for p in parts]
            count, strategy = len(posts), "page_text"
        return count, strategy, posts

    async def _collect_from_page(
        self,
        page: "Page",
        collected: dict[str, dict],
        *,
        label: str,
        max_new: int,
        max_scrolls: int,
        min_scrolls: int,
        on_status: StatusCallback | None = None,
    ) -> int:
        """Scroll the current page, adding de-duplicated posts (keyed by URN, else
        text) to `collected`, until: `max_new` new posts, `max_scrolls` scrolls, N
        scrolls with no new posts (after `min_scrolls`), or M empty reads. A post
        seen again keeps the longer text and records every place it was found.
        Returns the number of new posts. Cooperative-stop aware."""
        added_total = 0
        no_new_streak = 0
        empty_dom_streak = 0
        prev_height = 0

        await _wait_for_page_text_stable(page, stable_rounds=3, max_polls=30)

        for scroll_i in range(max_scrolls + 1):
            if run_guard.is_stop_requested():
                logger.info("linkedin_feed_collect_stopped_by_user", source=label, scroll=scroll_i)
                break

            await self._expand_posts(page)
            dom_count, strategy, posts = await self._read_page_posts(page)

            if dom_count == 0:
                empty_dom_streak += 1
                logger.info("linkedin_feed_empty_dom", source=label, scroll=scroll_i, streak=empty_dom_streak)
                if empty_dom_streak >= self._empty_dom_rounds:
                    logger.info("linkedin_feed_stop_empty_dom", source=label, scroll=scroll_i)
                    break
            else:
                empty_dom_streak = 0

            added = 0
            for p in posts:
                key = (p.get("urn") or "").strip() or _clean(p.get("full_text") or p.get("text", ""))[:120]
                if not key:
                    continue
                existing = collected.get(key)
                if existing is not None:
                    if len(p.get("full_text") or "") > len(existing.get("full_text") or ""):
                        p["found_via"] = existing.get("found_via", [])
                        existing = collected[key] = p
                    if label not in existing.setdefault("found_via", []):
                        existing["found_via"].append(label)
                    continue
                p["found_via"] = [label]
                collected[key] = p
                added += 1
                if added_total + added >= max_new:
                    break
            added_total += added
            no_new_streak = 0 if added else no_new_streak + 1

            if on_status:
                await on_status(f"Collecting posts ({label})… {len(collected)} gathered")
            logger.info(
                "linkedin_feed_scroll",
                source=label, scroll=scroll_i, dom_count=dom_count, strategy=strategy,
                new=added, total=len(collected), no_new_streak=no_new_streak,
            )

            if added_total >= max_new:
                logger.info("linkedin_feed_stop_max_posts", source=label, total=added_total)
                break
            # Honour the "no new posts" early-stop only after a minimum number of
            # scrolls: LinkedIn often serves a barren batch before loading more.
            if no_new_streak >= self._no_new_rounds and scroll_i >= min_scrolls:
                logger.info("linkedin_feed_stop_no_new", source=label, scroll=scroll_i)
                break
            if scroll_i >= max_scrolls:
                logger.info("linkedin_feed_stop_max_scrolls", source=label, scroll=scroll_i)
                break

            # If the document grew, the infinite-scroll loader fired; don't treat a
            # slow-rendering batch as the end of the page.
            new_height = await self._scroll(page)
            if new_height > prev_height + 50:
                no_new_streak = 0
            prev_height = new_height

        self._source_counts[label] = self._source_counts.get(label, 0) + added_total
        logger.info("linkedin_feed_collect_done", source=label, new=added_total, total=len(collected))
        return added_total

    async def _scroll(self, page: "Page") -> int:
        """Trigger the page's infinite-scroll loader and return the document scroll
        height afterwards (so the caller can detect real growth).

        Three nudges: scroll the document to the bottom, pull the last rendered post
        into view (works whichever ancestor actually scrolls), and a real wheel
        gesture over the viewport centre (a wheel at (0,0) lands on the fixed top
        nav). On search pages a "Show more results" button is clicked as well."""
        try:
            await page.evaluate(_SCROLL_JS)
        except Exception as exc:
            logger.debug("linkedin_feed_scroll_eval_failed", error=str(exc))
        try:
            await page.mouse.move(683, 450)
            await page.mouse.wheel(0, 3000)
        except Exception:
            pass
        await page.wait_for_timeout(self._scroll_delay_ms)
        try:
            return int(await page.evaluate(
                "() => (document.scrollingElement || document.documentElement).scrollHeight"
            ))
        except Exception:
            return 0

    # ── LLM processing ──────────────────────────────────────────────────────────

    async def _call_llm(self, fn: Callable[[], Awaitable[dict]]) -> dict:
        """Run one LLM call with retry. Retries `_LLM_POST_RETRIES` times with
        backoff on LLMUnavailableError (which also covers one unusable reply);
        re-raises when retries are exhausted. Resets the failure streak on success."""
        for attempt in range(_LLM_POST_RETRIES + 1):
            try:
                result = await fn()
                self._llm_failure_streak = 0
                return result
            except LLMUnavailableError:
                self._llm_failure_streak += 1
                if attempt >= _LLM_POST_RETRIES or self._llm_failure_streak >= _LLM_FAILURE_STREAK_ABORT:
                    raise
                await asyncio.sleep(_LLM_RETRY_BACKOFF_S * (attempt + 1))
        return {}

    async def _process_posts(
        self,
        raw_posts: list[dict],
        on_status: StatusCallback | None = None,
        on_batch: BatchCallback | None = None,
    ) -> list[FeedLead]:
        """Turn collected posts into vendor leads via filter -> LLM classify -> LLM extract.

        A cheap keyword filter runs first, so only candidate posts cost LLM calls.
        A post whose LLM call still fails after a retry is skipped (stage
        "llm_error"); processing stops on a user stop request, at the per-run LLM
        call cap, or after _LLM_FAILURE_STREAK_ABORT consecutive LLM failures. Posts
        never reached are recorded as "not_processed". Leads are handed to on_batch
        every _PERSIST_BATCH and at the end; a per-post diagnostic (feed_diag.json)
        is written.
        """
        leads: list[FeedLead] = []
        pending: list[FeedLead] = []
        diag: list[dict] = []
        cat_counts: dict[str, int] = {}
        candidates = 0
        classified_yes = 0
        stop_reason = ""
        last_idx = -1

        logger.info(
            "linkedin_feed_processing_start",
            posts=len(raw_posts), llm_calls_made=self._llm_calls_made,
            llm_max_calls=self._llm_max_calls, min_confidence=self._min_confidence,
            persist=on_batch is not None,
        )
        if on_status and raw_posts:
            await on_status(f"Classifying {len(raw_posts)} collected posts…")

        for idx, raw in enumerate(raw_posts):
            last_idx = idx
            if run_guard.is_stop_requested():
                stop_reason = "stopped_by_user"
                break

            text = _clean(raw.get("full_text") or raw.get("text", ""))
            rec: dict = {
                "idx":         idx,
                "author":      raw.get("author_name", ""),
                "urn":         raw.get("urn", ""),
                "found_via":   raw.get("found_via", []),
                "is_repost":   bool(raw.get("is_repost")),
                "is_promoted": bool(raw.get("is_promoted")),
                "text_len":    len(text),
                "snippet":     text[:280],
                "stage":       "collected",
            }

            if not _is_candidate(text):
                rec["stage"] = "filtered_out_not_candidate"
                diag.append(rec)
                logger.info(
                    "linkedin_feed_post_filtered", idx=idx, urn=rec["urn"][:40],
                    author=rec["author"][:60], text_len=len(text), snippet=text[:120],
                )
                continue
            candidates += 1
            logger.info(
                "linkedin_feed_post_candidate", idx=idx, urn=rec["urn"][:40],
                author=rec["author"][:60], found_via=rec["found_via"], snippet=text[:120],
            )

            if self._llm_calls_made >= self._llm_max_calls:
                logger.warning("linkedin_feed_llm_cap_reached", cap=self._llm_max_calls, idx=idx)
                rec["stage"] = "skipped_llm_cap"
                diag.append(rec)
                stop_reason = "llm_cap"
                break

            try:
                classification = await self._call_llm(lambda t=text, r=raw: self._classify(t, r))
                cat = str(classification.get("category") or "unknown")
                rec.update(
                    category=cat,
                    is_vendor_opportunity=classification.get("is_vendor_opportunity"),
                    it_relevance=classification.get("it_relevance"),
                    confidence=classification.get("confidence"),
                    reason=classification.get("reason"),
                )
                cat_counts[cat] = cat_counts.get(cat, 0) + 1
                keeper = self._is_keeper(classification)
                logger.info(
                    "linkedin_feed_post_classified", idx=idx, urn=rec["urn"][:40],
                    category=cat, keeper=keeper,
                    is_vendor_opportunity=classification.get("is_vendor_opportunity"),
                    it_relevance=classification.get("it_relevance"),
                    confidence=classification.get("confidence"),
                    reason=str(classification.get("reason") or "")[:200],
                    empty_reply=not classification,
                )
                if not keeper:
                    rec["stage"] = "classified_not_vendor_opportunity"
                    diag.append(rec)
                    continue
                classified_yes += 1

                if self._llm_calls_made >= self._llm_max_calls:
                    rec["stage"] = "skipped_llm_cap_before_extract"
                    diag.append(rec)
                    stop_reason = "llm_cap"
                    break

                extracted = await self._call_llm(lambda t=text, r=raw: self._extract(t, r))
                lead = self._finalize(raw, text, classification, extracted)
                if lead is None:
                    rec["stage"] = "dropped_empty_extraction"
                    diag.append(rec)
                    continue
                rec["stage"] = "lead"
                rec["lead_confidence"] = lead.lead_confidence
            except LLMUnavailableError as exc:
                rec["stage"] = "llm_error"
                rec["error"] = str(exc)[:300]
                diag.append(rec)
                if self._llm_failure_streak >= _LLM_FAILURE_STREAK_ABORT:
                    logger.error("linkedin_feed_llm_unavailable", error=str(exc), processed=idx)
                    stop_reason = "llm_unavailable"
                    break
                logger.warning("linkedin_feed_post_llm_error", idx=idx, error=str(exc))
                continue
            except Exception as exc:
                # One bad post must never abort the whole harvest.
                logger.warning(
                    "linkedin_feed_post_failed", idx=idx, urn=rec["urn"][:40],
                    error_type=type(exc).__name__, error=str(exc), exc_info=True,
                )
                rec["stage"] = "error"
                rec["error"] = str(exc)[:300]
                diag.append(rec)
                continue

            diag.append(rec)
            leads.append(lead)
            pending.append(lead)
            if on_batch and len(pending) >= _PERSIST_BATCH:
                await self._flush(pending, on_batch, on_status, len(leads))
                pending = []

        # Record posts never reached so the diagnostic accounts for every post.
        if stop_reason:
            first_unrecorded = last_idx if stop_reason == "stopped_by_user" else last_idx + 1
            for idx in range(first_unrecorded, len(raw_posts)):
                raw = raw_posts[idx]
                diag.append({
                    "idx": idx, "author": raw.get("author_name", ""),
                    "found_via": raw.get("found_via", []),
                    "snippet": _clean(raw.get("full_text") or raw.get("text", ""))[:280],
                    "stage": "not_processed",
                })

        if pending and on_batch:
            await self._flush(pending, on_batch, on_status, len(leads))

        logger.info(
            "linkedin_feed_processing_done",
            posts=len(raw_posts), candidates=candidates,
            classified_vendor_opportunity=classified_yes, leads=len(leads),
            categories=cat_counts, stop_reason=stop_reason or None,
        )
        self._write_diag(
            diag, cat_counts, len(raw_posts), candidates, classified_yes, len(leads),
            source_counts=dict(self._source_counts), stop_reason=stop_reason,
        )
        return leads

    @staticmethod
    def _write_diag(
        diag: list[dict], cat_counts: dict[str, int],
        posts: int, candidates: int, classified_yes: int, leads: int,
        source_counts: dict[str, int] | None = None, stop_reason: str = "",
    ) -> None:
        """Best-effort per-run diagnostic dump so a '0 leads' run is explainable:
        every collected post with its snippet, where it was found and the stage it
        stopped at, plus a summary (per-source counts, category histogram, why
        processing stopped early). Written to the bind-mounted data dir
        (ai-harvest-agent/data/results/lead_intelligence/feed_diag.json on the
        host). Never raises."""
        try:
            import json as _json
            import pathlib as _pl
            d = _pl.Path("data/results/lead_intelligence")
            d.mkdir(parents=True, exist_ok=True)
            (d / "feed_diag.json").write_text(
                _json.dumps(
                    {
                        "summary": {
                            "posts_collected":               posts,
                            "posts_by_source":               source_counts or {},
                            "candidates":                    candidates,
                            "classified_vendor_opportunity": classified_yes,
                            "leads":                         leads,
                            "category_counts":               cat_counts,
                            "stop_reason":                   stop_reason or None,
                        },
                        "posts": diag,
                    },
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
            logger.info("linkedin_feed_diag_written", path=str(d / "feed_diag.json"), summary_leads=leads)
        except Exception as exc:
            logger.warning("linkedin_feed_diag_write_failed", error=str(exc))

    async def _flush(
        self,
        pending: list[FeedLead],
        on_batch: BatchCallback,
        on_status: StatusCallback | None,
        total: int,
    ) -> None:
        """Hand a copy of the pending leads to on_batch and report the running saved count.

        Persistence errors are logged and swallowed so the harvest keeps going.
        """
        logger.info(
            "linkedin_feed_batch_flush", batch=len(pending), total_leads=total,
            titles=[lead.job_title[:60] for lead in pending],
        )
        try:
            await on_batch(list(pending))
            if on_status:
                await on_status(f"Extracting vendor leads… {total} saved")
        except Exception as exc:
            logger.warning(
                "linkedin_feed_batch_flush_failed", batch=len(pending),
                error_type=type(exc).__name__, error=str(exc), exc_info=True,
            )

    def _is_keeper(self, c: dict) -> bool:
        """Accept only a genuine vendor opportunity above the confidence gate. A
        vendor requirement must be for IT roles; a vendor registration only has to
        be for staffing / recruitment vendors (the classifier checks that)."""
        try:
            conf = float(c.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        cat = c.get("category")
        return bool(
            c.get("is_vendor_opportunity")
            and cat in _KEEPER_CATEGORIES
            and (c.get("it_relevance") or cat == "vendor_registration")
            and conf >= self._min_confidence
        )

    @staticmethod
    def _post_context(raw: dict) -> str:
        """Poster / flags / links header shared by the classify and extract prompts."""
        author = _clean(raw.get("author_name", ""))
        headline = _clean(raw.get("author_headline", ""))
        links = [str(x) for x in (raw.get("links") or []) if x][:10]
        return (
            f"---POSTER---\n{author or '(unknown)'} — {headline or '(no headline)'}\n"
            f"---PROMOTED POST---\n{'yes' if raw.get('is_promoted') else 'no'}\n"
            f"---LINKS IN POST---\n{chr(10).join(links) if links else '(none)'}\n"
        )

    # ── Stage 2: classification ─────────────────────────────────────────────────

    async def _classify(self, text: str, raw: dict) -> dict:
        """Ask the LLM whether a post is a vendor opportunity for an IT staffing
        vendor (one LLM call). Returns the classifier dict (is_vendor_opportunity,
        it_relevance, category, confidence, reason) or {} on an empty reply."""
        content = (
            f"{self._post_context(raw)}\n"
            f"---POST TEXT---\n{text[:_POST_TEXT_MAX_CHARS]}"
        )
        schema = (
            '{"is_vendor_opportunity": bool (true ONLY if the poster is seeking EXTERNAL '
            "staffing / recruitment / IT-services vendors or partners: vendor registration "
            "or empanelment, partner onboarding, or a contract / C2C / C2H / third-party "
            "requirement explicitly open to vendors, agencies or partners, so an IT staffing "
            "company could supply candidates or services. FALSE for a company hiring "
            "employees directly from candidates, a staffing firm advertising its own "
            "bench / hotlist / services, job seekers, commentary, and requests for suppliers "
            'of goods or non-staffing services), '
            '"it_relevance": bool (true if the roles or services are IT / software / '
            "technology: developers, data, cloud/DevOps, QA, SAP/Salesforce/ServiceNow, "
            "cybersecurity, IT support, etc.), "
            f'"category": one of {list(_FEED_CATEGORIES)} (vendor_registration = inviting '
            "staffing/recruitment vendors to register, empanel or partner; "
            "vendor_requirement = a specific requirement shared for vendors/partners to "
            "fill; direct_hiring = hiring candidates directly, no vendors; "
            "vendor_marketing = a staffing firm promoting its own consultants or services; "
            "non_staffing_vendor = seeking suppliers of goods or non-staffing services; "
            "ad_promo = an ad for a product, course or event; others as named), "
            '"confidence": number between 0 and 1 (your confidence in is_vendor_opportunity), '
            '"reason": str (one short sentence)}'
        )
        system = (
            "You screen LinkedIn posts for SightSpectrum, an IT staffing and recruitment "
            "vendor looking for companies that need staffing vendors. Decide from the "
            "SEMANTIC MEANING of the whole post, not keyword presence. 'We are empanelling "
            "recruitment agencies for our IT hiring, register here' and 'Need Java "
            "developers on C2C, vendors share profiles to x@y.com' ARE vendor opportunities. "
            "'We are hiring Java developers, apply here' is direct_hiring. 'Our bench: 5 "
            "Java consultants available for C2C' is vendor_marketing. A promoted post can "
            "still be a vendor opportunity. Return only the schema fields, no commentary."
        )
        self._llm_calls_made += 1
        result = await self._get_llm_service().extract_json(
            content=content,
            schema_description=schema,
            system=system,
            call_type=LlmCallType.FEED_CLASSIFY,
        )
        return result or {}

    # ── Stage 3: extraction (verbatim-only, no fabrication) ─────────────────────

    async def _extract(self, text: str, raw: dict) -> dict:
        """Ask the LLM to extract the opportunity and poster fields from a confirmed
        vendor-opportunity post (one LLM call).

        The posting date is resolved in Python first and passed in (stored back as
        "_resolved_date"); the prompt forbids invented companies/emails/phones/links.
        The exact content sent is stored as "_source_text" so _finalize can ground
        the returned email/phone/link against it."""
        profile_url = (raw.get("author_profile_url") or "").strip()
        mailto = _clean_mailto(raw.get("mailto"))

        resolved_date = _resolve_feed_posted_date(raw, text, self._harvest_started_at)

        schema = (
            '{"hiring_company": str (the company that needs vendors or candidates: the end '
            "client if named, else the poster's company; empty if not stated — do NOT guess), "
            '"requester_type": "end_client" | "staffing_firm" | "unknown" (end_client = the '
            "company itself needs the people/services; staffing_firm = a staffing firm or "
            "prime vendor passing on its client's requirement), "
            '"opportunity_type": "vendor_registration" | "vendor_requirement", '
            '"engagement_model": str (e.g. C2C, C2H, Contract, Third-party payroll, Vendor '
            "empanelment, Staff augmentation; empty if not stated), "
            '"roles": list[str] (roles / positions needed, as written; empty list if none), '
            '"positions_count": str (empty if not stated), '
            '"location": str (city/region and Remote/Hybrid/On-site if stated; empty otherwise), '
            '"duration": str (contract duration; empty if not stated), '
            '"rate_or_budget": str (empty if not disclosed), '
            '"skills": list[str] (technologies/skills explicitly named; empty list if none), '
            '"requirement_summary": str (1-2 sentences: what they need from vendors, using '
            "only facts in the post), "
            '"how_to_engage": str (how vendors should respond, as stated, e.g. "share '
            'profiles to …", "fill the registration form"; empty if not stated), '
            '"registration_link": str or null (a URL copied EXACTLY from the post text or the '
            "LINKS list; null if none), "
            '"deadline": str (empty if not stated), '
            '"posted": str or null (copy the pre-calculated date given below verbatim; '
            "do not derive it yourself; null if marked unavailable), "
            '"recruiter_name": str or null (the poster, null if unknown), '
            '"recruiter_title": str or null (their designation, null if unknown), '
            '"recruiter_company": str or null (only if explicitly stated), '
            '"recruiter_email": str or null (ONLY if an email appears verbatim in the '
            "text — never guess or construct one; if masked/partial, null), "
            '"recruiter_phone": str or null (ONLY if a phone number appears verbatim — '
            "never guess; E.164 with country code when unambiguous; if masked/partial, null), "
            '"market": "US" | "IN" | "unknown" (the ONE field you may judge rather than copy: '
            "which client market this opportunity is for. US = United States/Canada market "
            "(USD or $/hr rates, W2 / C2C / 1099 / H1B / GC / USC / EAD / OPT visa terms, "
            "EST/CST/PST time zones, US cities/states, US-style phone numbers). IN = India "
            "(INR / LPA / lakh / crore, Indian cities/states, IST, +91 numbers, notice period, "
            "PF / third-party payroll in India). unknown when the cues are absent or mixed)}"
        )
        system = (
            "You extract structured data from a single LinkedIn post in which a company "
            "is looking for staffing / recruitment vendors. Extract ONLY what is "
            "explicitly present. NEVER fabricate, infer, or construct a company, email, "
            "phone, link, location, rate, or requirement that is not literally in the "
            "text (e.g. do not build an email from a name and company). If a field is not "
            "present, return empty/null. Return only the schema fields, no commentary."
        )
        content = (
            f"{self._post_context(raw)}"
            f"---POSTER PROFILE URL---\n{profile_url or '(none)'}\n"
            f"---MAILTO LINK---\n{mailto or '(none)'}\n"
            f"---POSTING DATE (pre-calculated)---\n{resolved_date or 'unavailable'}\n\n"
            f"---POST TEXT---\n{text[:_POST_TEXT_MAX_CHARS]}"
        )
        self._llm_calls_made += 1
        result = await self._get_llm_service().extract_json(
            content=content,
            schema_description=schema,
            system=system,
            call_type=LlmCallType.FEED_EXTRACT,
        )
        result = result or {}
        result["_resolved_date"] = resolved_date
        result["_source_text"] = content
        return result

    # ── Validate + confidence ───────────────────────────────────────────────────

    def _finalize(
        self, raw: dict, text: str, classification: dict, extracted: dict,
    ) -> FeedLead | None:
        """Build a validated FeedLead, or None when the extraction came back empty.

        The LLM's email/phone/registration link are kept only when grounded in the
        content it was sent (extracted["_source_text"], else the post text + mailto)
        or the post's own links; a DOM mailto address is the email fallback."""
        # "market" is always filled ("unknown" at worst) — it doesn't make an empty
        # extraction non-empty.
        if not any(
            not str(k).startswith("_") and k != "market" and v for k, v in extracted.items()
        ):
            logger.info("linkedin_feed_lead_dropped_empty_extraction", urn=str(raw.get("urn", ""))[:40])
            return None

        def _s(key: str) -> str:
            return str(extracted.get(key) or "").strip()

        def _list(key: str, limit: int) -> list[str]:
            vals = extracted.get(key) or []
            if isinstance(vals, str):
                vals = [vals]
            return [str(v).strip() for v in vals if str(v).strip()][:limit]

        category = str(classification.get("category") or "")
        opportunity = _s("opportunity_type")
        if opportunity not in _KEEPER_CATEGORIES:
            opportunity = category if category in _KEEPER_CATEGORIES else "vendor_requirement"
        requester = _s("requester_type").lower()
        if requester not in ("end_client", "staffing_firm"):
            requester = "unknown"

        roles = _list("roles", 10)
        if roles:
            job_title = ", ".join(roles[:3])[:250]
        elif opportunity == "vendor_registration":
            job_title = "Vendor empanelment / registration"
        else:
            job_title = "Contract staffing requirement"

        # Poster identity: prefer the DOM-scraped author over the LLM's echo.
        author_name = _clean(raw.get("author_name", "")) or _s("recruiter_name")
        profile_url = (raw.get("author_profile_url") or "").split("?")[0].strip()
        if profile_url and "/in/" not in profile_url.lower():
            profile_url = ""  # company/school link, not a person's profile

        # Grounding: an LLM-returned contact must be written in what it was given.
        mailto = _clean_mailto(raw.get("mailto"))
        source_text = extracted.get("_source_text") or text
        sources = [source_text, mailto]
        email = ground_email(normalize_email(extracted.get("recruiter_email")), sources)[0]
        phone = ground_phone(normalize_phone(extracted.get("recruiter_phone")), sources)[0]
        email = normalize_email(email) or ""
        phone = normalize_phone(phone) or ""
        if not email and mailto:
            email = normalize_email(mailto) or ""
        links = [str(x) for x in (raw.get("links") or []) if x]
        registration_link = _ground_link(extracted.get("registration_link"), links, f"{text} {source_text}")

        company = _s("hiring_company") or _s("company")
        location = _s("location")
        skills = _list("skills", 20)
        engagement = _s("engagement_model")
        how_to_engage = _s("how_to_engage")
        posted = extracted.get("_resolved_date") or _s("posted")

        details = [
            ("Engagement", engagement),
            ("Roles", ", ".join(roles)),
            ("Positions", _s("positions_count")),
            ("Duration", _s("duration")),
            ("Rate / budget", _s("rate_or_budget")),
            ("Deadline", _s("deadline")),
            ("How to engage", how_to_engage),
            ("Registration link", registration_link),
        ]
        body = _clean(raw.get("text") or "") or text
        description = "\n".join(
            [_s("requirement_summary")]
            + [f"{k}: {v}" for k, v in details if v]
            + ["", "--- Original post ---", body[:_POST_TEXT_MAX_CHARS]]
        ).strip()[:20_000]

        domain = _classify_hiring_domain("", _clean(raw.get("author_headline", "")), [job_title, description])
        domain = (domain.split(",")[0].strip() if domain and domain != "NOT_FOUND" else "IT")

        lead = FeedLead(
            job_title=job_title,
            company=company,
            location=location,
            salary=_s("rate_or_budget"),
            employment_type=engagement,
            work_mode=_infer_work_mode(f"{location} {description}"),
            skills=skills,
            description=description,
            posted_date=posted,
            domain=domain,
            opportunity_type=opportunity,
            requester_type=requester,
            how_to_engage=how_to_engage,
            registration_link=registration_link,
            recruiter_name=author_name,
            recruiter_title=_s("recruiter_title") or _clean(raw.get("author_headline", "")),
            recruiter_company=_s("recruiter_company"),
            recruiter_profile_url=profile_url,
            recruiter_email=email,
            recruiter_phone=phone,
            post_url=self._permalink(raw.get("urn", "")),
            post_urn=str(raw.get("urn") or ""),
            is_repost=bool(raw.get("is_repost")),
            is_promoted=bool(raw.get("is_promoted")),
            found_via=list(raw.get("found_via") or []),
            category=opportunity,
            market=normalize_market(extracted.get("market")),
        )
        lead.lead_confidence = self._score(lead, classification, text, roles)
        logger.info(
            "linkedin_feed_lead",
            title=lead.job_title, company=lead.company, type=lead.opportunity_type,
            poster=lead.recruiter_name, has_email=bool(lead.recruiter_email),
            has_phone=bool(lead.recruiter_phone), has_link=bool(lead.registration_link),
            confidence=lead.lead_confidence,
        )
        return lead

    def _score(self, lead: FeedLead, classification: dict, text: str, roles: list[str]) -> float:
        """Blend the classifier's own confidence with a count of independent
        supporting signals — a lead is stronger when several agree. Each signal is a
        real, present piece of evidence."""
        try:
            llm_conf = float(classification.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            llm_conf = 0.0
        llm_conf = max(0.0, min(1.0, llm_conf))

        signals = [
            bool(_VENDOR_STRONG_RE.search(text or "")),            # explicit vendor language
            bool(lead.company),                                    # who needs the vendors
            bool(roles) or lead.opportunity_type == "vendor_registration",  # concrete ask
            bool(lead.how_to_engage or lead.registration_link
                 or lead.recruiter_email or lead.recruiter_phone),  # a way to respond
            bool(lead.recruiter_name),                             # poster identity
            bool(classification.get("it_relevance")),              # IT relevance
        ]
        signal_fraction = sum(1 for s in signals if s) / len(signals)
        return round(0.5 * llm_conf + 0.5 * signal_fraction, 3)

    @staticmethod
    def _permalink(urn: str) -> str:
        """Build the post URL from an activity / ugcPost / share URN ("" otherwise)."""
        urn = (urn or "").strip()
        if re.fullmatch(r"urn:li:(?:activity|ugcPost|share):\d+", urn):
            return f"https://www.linkedin.com/feed/update/{urn}/"
        return ""

    def _get_llm_service(self) -> "LLMService":
        """The LLM service, created on first use when none was injected. (This method
        was accidentally removed in f43447c, which made every classify call raise
        AttributeError and every feed run end with 0 leads.)"""
        if self._llm_service is None:
            from app.services.llm_service import LLMService
            self._llm_service = LLMService(self._settings)
        return self._llm_service
