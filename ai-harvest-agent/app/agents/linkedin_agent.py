"""
LinkedIn Harvest Agent — employer-authenticated job scraper.

Rules
─────
• NEVER fall back to guest/anonymous mode.
• Session file (data/config/linkedin_session.json) is always tried first.
• If the session is expired and credentials are available, re-login.
• If login fails for any reason → raise LinkedInLoginError immediately.
• Debug screenshots are saved to debug/ at every stage.
• HTML snapshots are saved to debug/ on any error.

Credentials
───────────
Loaded from .env via Settings (LINKEDIN_EMAIL / LINKEDIN_PASSWORD).
Never hardcoded anywhere in this file.

Filter URL parameters
─────────────────────
work_mode  Remote → f_WT=2  |  Hybrid → f_WT=3  |  Onsite → f_WT=1
job_type   Contract → f_JT=C  |  Permanent → f_JT=F  |  Part-time → f_JT=P
date       24h → r86400  |  week → r604800  |  month → r2592000
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable
from urllib.parse import quote_plus

import structlog
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser
from playwright.async_api import ElementHandle, Page

from app.core.exceptions import LLMUnavailableError
from app.core.text_formatting import (
    description_text_to_html,
    format_job_description,
    sanitize_description_html,
)
from app.models.harvest_models import FiltersConfig
from app.scrapers.browser_manager import PersistentBrowserManager

if TYPE_CHECKING:
    from app.services.llm_service import LLMService

logger = structlog.get_logger(__name__)

LINKEDIN_SESSION_FILE = Path("data/sessions/linkedin_session.json")
_DEBUG_DIR            = Path("data/debug/linkedin")

# How long to pause a manually-triggered harvest waiting for a human to
# complete LinkedIn login via the live browser view before giving up.
_LOGIN_WAIT_TIMEOUT_S = 600
_LOGIN_WAIT_POLL_S    = 2

StatusCallback = Callable[[str], Awaitable[None]]

# Canonical jobs deep-link. LinkedIn resolves the free-text `location=` param
# into the correct geoId server-side on THIS path and applies it. The
# `/jobs/search-results/` SPA route does NOT — given only location text (no
# geoId) it silently falls back to the logged-in account's home location
# (e.g. Chennai), ignoring location=india. Keep this as /jobs/search/.
_LINKEDIN_SEARCH_URL = "https://www.linkedin.com/jobs/search/?"

# Job-description container selectors, in priority order — reused from the
# proven chain in app/scrapers/linkedin_scraper.py::_Sel.DESCRIPTION. Used to
# capture the container's inner HTML (rich formatting) for job_description_html.
_DESCRIPTION_SELECTORS: tuple[str, ...] = (
    "div.show-more-less-html__markup",          # public /jobs/view/
    "div#job-details",                          # alternate public layout
    "div.description__text",                    # authenticated view
    "section.description .description__text",
    "article.jobs-description__container",
    "div[class*='description__text']",
    "div[class*='job-view-layout'] div[class*='details']",
)

# URLs that indicate we are NOT logged in
_GATED_PATHS = ("/login", "/checkpoint", "/challenge", "/authwall", "/uas/", "login.live.com", "login.microsoftonline.com")


# ── Custom exception ───────────────────────────────────────────────────────────

class LinkedInLoginError(RuntimeError):
    """Raised when LinkedIn authentication fails. Scraping is aborted."""


# ── Filter → URL param maps ───────────────────────────────────────────────────

_WORK_MODE_MAP: dict[str, str] = {
    "Remote": "2",
    "Hybrid": "3",
    "Onsite": "1",
    "Any":    "",
}
_JOB_TYPE_MAP: dict[str, str] = {
    "Contract":   "C",
    "Permanent":  "F",
    "Part-time":  "P",
    "Full-time":  "F",
    "Freelance":  "T",
    "Any":        "",
}
_DATE_MAP: dict[int, str] = {
    24:  "r86400",
    168: "r604800",
    720: "r2592000",
}

# ── Scraped job dataclass ─────────────────────────────────────────────────────
  
@dataclass
class LinkedInScrapedJob:
    job_title:       str
    company:         str
    location:        str
    salary:          str
    experience:      str
    posted_date:     str
    job_url:         str
    job_description: str
    job_description_html: str  = ""   # sanitized rich HTML of the description container
    skills:          list[str] = field(default_factory=list)
    work_mode:       str       = "not_specified"
    company_url:     str       = ""
    employment_type: str       = ""
    industry_hint:   str       = ""   # raw "Industries" job-insight text, if scraped
    source:          str       = "LinkedIn"
    # Lead intelligence
    job_poster_name:        str | None = None
    job_poster_designation: str | None = None
    linkedin_profile_url:   str | None = None
    job_poster_company:     str | None = None
    job_poster_email:       str | None = None
    job_poster_phone:       str | None = None


# ── CSS selector fallback chains ──────────────────────────────────────────────

class _Sel:
    # Login form
    LOGIN_EMAIL: list[str] = [
        "input#username",
        "input[name='session_key']",
        "input[autocomplete='username']",
        "input[type='email']",
        "input[type='text']",
    ]
    LOGIN_PASSWORD: list[str] = [
        "input#password",
        "input[name='session_password']",
        "input[autocomplete='current-password']",
        "input[type='password']",
    ]
    LOGIN_SUBMIT: list[str] = [
        "button[type='submit'][data-litms-control-urn*='sign-in']",
        "button[type='submit'].btn__primary--large",
        "button[type='submit']",
        "button:has-text('Sign in')",
    ]

    # Authenticated nav indicators
    AUTH_AVATAR: list[str] = [
        "img.global-nav__me-photo",
        "img[class*='global-nav__me-photo']",
        "[data-control-name='nav.settings']",
        "a[href*='/in/'][aria-label]",
    ]
    AUTH_NAV: list[str] = [
        "nav[aria-label='Primary']",
        "div.global-nav__content",
        "ul.global-nav__primary-items",
        "nav.global-nav",
    ]

    # Overlays
    COOKIE: list[str] = [
        'button[action-type="ACCEPT"]',
        'button[data-control-name="ga-cookie-accept"]',
        'button:has-text("Accept cookies")',
        'button:has-text("Accept")',
    ]
    MODAL_DISMISS: list[str] = [
        'button[data-tracking-control-name="public_jobs_guest-alert-dismiss"]',
        'button.modal__dismiss',
        'button[aria-label="Dismiss"]',
        'div[role="dialog"] button[aria-label="Close"]',
        'button:has-text("Not now")',
    ]

    # Result container + cards (authenticated view)
    CONTAINER: list[str] = [
        "div.jobs-search-results-list",
        "ul.jobs-search__results-list",
        "ul[class*='jobs-search-results__list']",
        ".scaffold-layout__list-container",
        ".scaffold-layout__list",
        # Structural fallback, independent of whatever CSS classnames LinkedIn's
        # authenticated app currently uses — a job-search results list always
        # contains job permalinks, so find the closest <ul> wrapping them.
        "ul:has(a[href*='/jobs/view/'])",
    ]
    CARD: list[str] = [
        # LinkedIn's current split-view results render each card as a
        # `<div role="button" componentkey="job-card-component-ref-<jobId>">`
        # with fully hashed/atomic utility classes and no <li>/<a href> at
        # all — confirmed via a live DOM diagnosis dump (see
        # linkedin_job_cards_not_found history). componentkey is a functional
        # wiring attribute LinkedIn's own React tree depends on, so it's far
        # more stable than any classname. Tried first since it's the current
        # markup; the rest are kept as fallbacks for older/other templates.
        "[componentkey^='job-card-component-ref-']",
        "li[data-occludable-job-id]",
        "li.jobs-search-results__list-item",
        "ul.jobs-search__results-list > li",
        "div.base-card",
        "li[class*='jobs-search-results']",
        # Attribute/structure-based fallbacks that don't depend on LinkedIn's
        # (frequently renamed) utility CSS classnames. data-entity-urn carries
        # the job posting's internal URN and is a functional attribute LinkedIn's
        # own tracking code depends on, so it's far more stable than styling
        # classes. The href-based :has() fallback works regardless of markup
        # as long as the card links to its own job-view permalink.
        "div[data-entity-urn*='jobPosting']",
        "div[data-job-id]",
        "li:has(a[href*='/jobs/view/'])",
    ]

    # Card list-view fields
    TITLE:    list[str] = [
        "a.job-card-list__title--link",     # authenticated 2024+
        "a.job-card-list__title",
        "strong a",
        "[class*='job-card-list__title']",
        "h3.base-search-card__title",
        "h3",
        "h2",
    ]
    COMPANY:  list[str] = [
        "span.job-card-container__primary-description",  # authenticated 2024+
        ".job-card-container__primary-description",      # without tag qualifier
        "a.job-card-container__company-name",
        ".job-card-container__company-name",
        ".artdeco-entity-lockup__subtitle",              # newer LinkedIn structure
        "h4.base-search-card__subtitle",
        "h4 a",
        "h4",
    ]
    LOCATION: list[str] = [
        "span.job-card-container__metadata-item",
        "li.job-card-container__metadata-item",
        ".job-card-container__metadata-wrapper",
        "span.job-search-card__location",
        "[class*='metadata-item']",
        "[class*='location']",
    ]
    LINK:     list[str] = [
        "a.job-card-list__title--link",     # authenticated 2024+
        "a.job-card-list__title",
        "a.base-card__full-link",
        "a[href*='/jobs/view/']",
    ]
    POSTED:   list[str] = ["time", "span.job-search-card__listdate", "[class*='listdate']"]

    # Detail page — only used to expand the truncated description before
    # capturing the page's full text for LLM extraction (see
    # LinkedInAgent._llm_fallback_extract). Every other detail-page field is
    # extracted by the LLM rather than matched by selector, since LinkedIn's
    # detail-page CSS classes drift/obfuscate too often to rely on.
    SHOW_MORE_BTN:     list[str] = [
        'button[aria-label="Show more, visually expands previously read content"]',
        'button.show-more-less-html__button',
        'button:has-text("Show more")',
    ]


# ── Helpers ───────────────────────────────────────────────────────────────────

_UNICODE_JUNK = re.compile(r"[ ​‌‍﻿­]")
_WHITESPACE   = re.compile(r"[ \t]{2,}")


def _clean(text: str) -> str:
    text = _UNICODE_JUNK.sub(" ", text)
    text = _WHITESPACE.sub(" ", text)
    return re.sub(r"\n+", " ", text).strip()


# Known LinkedIn upsell/ad lines that show up in the *main content* area
# (not nav/header/footer, which are stripped as whole tags below) — pure
# noise for job-data extraction, and a big share of a captured page's text.
# Confirmed live: these more than doubled a detail page's prompt size and
# pushed a local LLM over its response-time budget for no extraction gain.
_LINKEDIN_BOILERPLATE_LINE = re.compile(
    r"Reactivate Premium|Job search smarter with Premium|"
    r"Premium members are up to|Cancel anytime\.|"
    r"Use AI to assess how you fit|Get AI-powered advice on this job|"
    r"Hiring, not job hunting\?|No response insights available|"
    r"Select language|"
    r"Skip to search|Skip to main content|Skip to primary content|"
    r"Skip to aside|Skip to footer|"
    r"notifications|"
    r"Show match details|Tailor my resume|Help me stand out|"
    r"Actively reviewing applicants|Promoted by hirer|"
    r"Be an early applicant|Easy Apply|Save$|"
    r"Access company insights|headcount trends|"
    r"members use Premium",
    re.IGNORECASE,
)

# Section headers LinkedIn always renders AFTER the real job content on a
# detail page (a related-jobs rail, "People also viewed", Premium upsells) —
# hundreds of lines of noise that add nothing for extraction. Cutting the
# text at the earliest of these alone shrinks a typical detail-page prompt
# by 30-50%.
_NOISE_CUTOFF_MARKERS = [
    "More jobs",
    "See more jobs like this",
    "More jobs like this",
    "Similar jobs",
    "People also viewed",
    "Job search faster with Premium",
    "Set alert for similar jobs",
]


def _html_to_text(html: str) -> str:
    """Strip tags/classes/scripts/styles from a full page's HTML, leaving
    plain text — used to feed the LLM extractor instead of matching
    LinkedIn's constantly-drifting CSS selectors. Unlike innerText, this also
    picks up text LinkedIn hides via CSS (e.g. a "Meet the hiring team" card
    that's present in the DOM but display:none for guest/unauthenticated
    sessions).

    Also strips whole-tag site chrome (nav/header/footer/aside — top nav,
    footer/legal/language links, and the related-jobs rail; never the job
    posting itself), known upsell/ad lines that live in the main content
    area, and truncates at the first noise-section marker (see
    _NOISE_CUTOFF_MARKERS) — LinkedIn always renders these after the real
    job content, so anything from there on is safe to drop."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer", "aside"]):
        tag.decompose()
    lines = soup.get_text(separator="\n").split("\n")
    lines = [ln for ln in lines if not _LINKEDIN_BOILERPLATE_LINE.search(ln)]
    text = _clean("\n".join(lines))

    cutoff_positions = [pos for pos in (text.find(marker) for marker in _NOISE_CUTOFF_MARKERS) if pos != -1]
    if cutoff_positions:
        text = text[: min(cutoff_positions)]
    return text


def _trim_to_relevant(text: str, max_chars: int) -> str:
    """Cap `text` at max_chars without silently cutting away the actual job
    description when it's preceded by a long related-jobs/recommendation
    rail. A plain head-cut can land entirely inside that rail on a page
    where "About the job" sits deep in the text — instead, keep a short
    lead-in (title/company/insights from the top of the page) then jump
    straight to the description section for the remaining budget."""
    if len(text) <= max_chars:
        return text
    head_budget = min(1_500, max_chars // 4)
    idx = text.find("About the job")
    if idx == -1 or idx <= head_budget:
        return text[:max_chars]
    head = text[:head_budget]
    body = text[idx: idx + (max_chars - head_budget)]
    return f"{head}\n…\n{body}"


# Marker for the start of a LinkedIn profile's "Activity" section — the member's
# posts/reposts/comments feed. Crucially this feed also surfaces OTHER people's
# content (reposts of colleagues' hiring posts, comments) that carry THEIR own
# emails/phones. The profile section header reads "Activity <n> followers",
# which is specific enough not to collide with the word "Activity" appearing
# inside a headline or About paragraph.
_PROFILE_ACTIVITY_CUTOFF_RE = re.compile(r"\bActivity\s+[\d,]+\s+followers?\b", re.IGNORECASE)


def _cut_profile_text_before_activity(text: str) -> str:
    """Drop everything from a LinkedIn profile's Activity feed onward.

    On a profile page the top card + contact info + About come first, then the
    Activity section — reposts/comments that belong to OTHER members. Only the
    region before Activity is the profile owner's own data, so the recruiter-
    contact LLM fallback must never see past it: otherwise another member's
    email/phone from a repost gets extracted and saved as THIS person's contact
    (confirmed live — a profile with no public contact fell through to this
    fallback and the feed leaked colleagues' addresses into the prompt)."""
    m = _PROFILE_ACTIVITY_CUTOFF_RE.search(text)
    if m:
        return text[: m.start()].rstrip()
    return text


def _infer_work_mode(text: str) -> str:
    t = (text or "").lower()
    if "remote" in t:
        return "remote"
    if "hybrid" in t:
        return "hybrid"
    if "on-site" in t or "onsite" in t or "in office" in t:
        return "onsite"
    return "not_specified"


def _format_posted(raw: str) -> str:
    if not raw:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw = raw.strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    if "T" in raw and len(raw) >= 10:
        return raw[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


_ABSOLUTE_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_RELATIVE_DATE_RE = re.compile(
    r"(?P<num>\d+)\s*(?P<unit>hours?|h|days?|d|weeks?|w|months?)\s*ago"
    r"|(?P<yesterday>yesterday)"
    r"|(?P<now>just now|moments ago)",
    re.IGNORECASE,
)


def _resolve_posted_date(raw_text: str, harvest_date: datetime) -> str | None:
    """Convert a relative LinkedIn posting-date string ("3 days ago", "2w
    ago", "yesterday", …) to an absolute YYYY-MM-DD date computed here in
    Python, before the text ever reaches the LLM — the LLM was previously
    asked to do this arithmetic itself, which cost tokens and occasionally
    got the date wrong. Returns None on no match, leaving the caller to fall
    back to the LLM's own best-effort read of the surrounding text.

    Standalone module-level function (not a method) — takes harvest_date
    explicitly so it stays a pure, independently testable function rather
    than reaching for "now" or agent instance state itself.
    """
    if not raw_text:
        return None

    abs_match = _ABSOLUTE_DATE_RE.search(raw_text)
    if abs_match:
        return abs_match.group(0)

    m = _RELATIVE_DATE_RE.search(raw_text)
    if m:
        if m.group("yesterday"):
            delta_days = 1
        elif m.group("now"):
            delta_days = 0
        else:
            num  = int(m.group("num"))
            unit = m.group("unit").lower()
            if unit.startswith("h"):
                delta_days = 0
            elif unit.startswith("d"):
                delta_days = num
            elif unit.startswith("w"):
                delta_days = num * 7
            elif unit.startswith("month"):
                delta_days = num * 30
            else:
                return None
        return (harvest_date - timedelta(days=delta_days)).strftime("%Y-%m-%d")

    try:
        parsed = dateutil_parser.parse(raw_text, fuzzy=True, default=harvest_date)
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=harvest_date.tzinfo)
    if parsed > harvest_date + timedelta(days=1) or parsed < harvest_date - timedelta(days=365 * 2):
        return None
    return parsed.strftime("%Y-%m-%d")


_COMPANY_NAME_PUNCT_RE = re.compile(r"[^\w\s]")


def _normalize_company_name(name: str) -> str:
    name = _COMPANY_NAME_PUNCT_RE.sub("", (name or "").lower())
    return re.sub(r"\s+", " ", name).strip()


def _resolve_company_url_from_links(company_name: str, company_links: list[dict]) -> str:
    """Fallback for when the LLM returned an empty/wrong company_url despite
    a clear candidate being available: match the extracted company name
    against each candidate link's own text (both normalized — lowercased,
    punctuation stripped), accepting an exact match or either string
    containing the other. All candidates already come from `a[href*='/company/']`
    (see _llm_fallback_extract), so any match is already a canonical company
    URL — this just strips a trailing "/life" (LinkedIn's culture/life-page
    tab) to land on the company's main page instead of that sub-tab."""
    target = _normalize_company_name(company_name)
    if not target:
        return ""
    for link in company_links:
        link_name = _normalize_company_name(link.get("text", ""))
        if not link_name:
            continue
        if link_name == target or link_name in target or target in link_name:
            href = (link.get("href") or "").rstrip("/")
            if href.endswith("/life"):
                href = href[: -len("/life")]
            return href
    return ""


def _test_mode_max_jobs() -> int | None:
    """
    Optional testing cap read from LINKEDIN_TEST_MAX_JOBS in the environment
    (.env). When set to a positive integer, it overrides FiltersConfig.max_jobs
    with a lower value so a test run doesn't harvest the full result set.
    Unset/blank/non-positive → no override (normal max_jobs behaviour).
    """
    raw = os.getenv("LINKEDIN_TEST_MAX_JOBS", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning("linkedin_test_max_jobs_invalid", value=raw)
        return None
    return value if value > 0 else None


async def _delay(page: Page, lo: int, hi: int) -> None:
    await page.wait_for_timeout(random.randint(lo, hi))


async def _wait_for_page_text_stable(
    page:          Page,
    poll_ms:       int = 500,
    stable_rounds: int = 4,
    max_polls:     int = 90,
) -> int:
    """
    Poll document.body.innerText.length until it stops growing for
    `stable_rounds` consecutive checks in a row (not just one lucky
    plateau — a client-rendered SPA can pause mid-hydration) before
    treating the page as "done rendering". Used before handing the page to
    the LLM extractor — a fixed short delay isn't reliable here: a capture
    taken too early on this SPA can land with only nav/footer chrome and no
    actual job content, even though the same page finishes rendering a
    moment later (confirmed live via a saved debug HTML/LLM-prompt pair).

    Bounded at `max_polls` * `poll_ms` (default ~45s) rather than an
    unbounded wait — some pages carry a live/ticking element (relative
    timestamps, view counters) whose text never truly stops changing, and
    an unbounded wait there would hang the entire harvest run on one page.
    Returns the final text length observed (for logging), whether or not
    it actually stabilized.
    """
    prev_len = -1
    stable   = 0
    for _ in range(max_polls):
        try:
            cur_len = await page.evaluate("() => document.body.innerText.length")
        except Exception:
            break
        if cur_len == prev_len:
            stable += 1
            if stable >= stable_rounds:
                break
        else:
            stable = 0
        prev_len = cur_len
        await page.wait_for_timeout(poll_ms)
    return prev_len


async def _first_text(root: Page | ElementHandle, selectors: list[str]) -> str:
    for sel in selectors:
        try:
            el = await root.query_selector(sel)
            if el:
                text = await el.inner_text()
                if text and text.strip():
                    return text.strip()
        except Exception:
            continue
    return ""


async def _first_attr(root: Page | ElementHandle, selectors: list[str], attr: str) -> str:
    for sel in selectors:
        try:
            el = await root.query_selector(sel)
            if el:
                val = await el.get_attribute(attr)
                if val:
                    return val.strip()
        except Exception:
            continue
    return ""


def _ensure_debug_dir() -> Path:
    _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    return _DEBUG_DIR


async def _screenshot(page: Page, name: str) -> None:
    """Save a debug screenshot to debug/<name>.png (silently ignores errors)."""
    try:
        d = _ensure_debug_dir()
        await page.screenshot(path=str(d / f"{name}.png"), full_page=False)
        logger.debug("debug_screenshot_saved", name=name)
    except Exception as exc:
        logger.debug("debug_screenshot_failed", name=name, error=str(exc))


async def _save_html(page: Page, name: str) -> None:
    """Save full page HTML to debug/<name>.html (silently ignores errors)."""
    try:
        d = _ensure_debug_dir()
        content = await page.content()
        (d / f"{name}.html").write_text(content, encoding="utf-8")
        logger.debug("debug_html_saved", name=name)
    except Exception as exc:
        logger.debug("debug_html_failed", name=name, error=str(exc))


_DOM_WALK_JS = r"""
() => {
    const results = {
        light_dom_job_links: [], shadow_hosts: [], shadow_job_links: [],
        role_matches: [], aria_job_labels: [], li_count: 0, theme_host_detail: null,
    };
    function describe(el) {
        if (!el) return null;
        const attrs = {};
        for (const a of el.attributes) attrs[a.name] = a.value;
        return { tag: el.tagName.toLowerCase(), attrs };
    }
    function ancestorChain(el, depth) {
        const chain = [];
        let cur = el;
        for (let i = 0; i < depth && cur; i++) { chain.push(describe(cur)); cur = cur.parentElement; }
        return chain;
    }
    const links = document.querySelectorAll('a[href*="/jobs/view/"]');
    links.forEach((a, i) => { if (i < 5) results.light_dom_job_links.push(ancestorChain(a, 8)); });
    results.light_dom_job_link_count = links.length;

    // Anchor hrefs may not exist for list cards at all in the redesigned SPA
    // (client-side router on a non-anchor element) — ARIA roles/labels are
    // driven by accessibility requirements, not the CSS-module build, so
    // they tend to survive even when every classname is hashed.
    const ROLE_TARGETS = new Set(['listitem', 'option', 'article', 'button', 'link']);
    function scan(root, path) {
        const all = root.querySelectorAll('*');
        for (const el of all) {
            if (el.tagName === 'LI') results.li_count++;
            const role = el.getAttribute && el.getAttribute('role');
            if (role && ROLE_TARGETS.has(role) && results.role_matches.length < 15) {
                results.role_matches.push({ path, role, chain: ancestorChain(el, 4) });
            }
            const ariaLabel = el.getAttribute && el.getAttribute('aria-label');
            if (ariaLabel && /job/i.test(ariaLabel) && results.aria_job_labels.length < 15) {
                results.aria_job_labels.push({ path, ariaLabel, chain: ancestorChain(el, 4) });
            }
            if (el.shadowRoot) {
                results.shadow_hosts.push({ tag: el.tagName.toLowerCase(), class: el.className, path });
                if (el.className && el.className.includes('theme--light') && !results.theme_host_detail) {
                    results.theme_host_detail = {
                        light_dom_children: Array.from(el.children).slice(0, 8).map(c => ({ tag: c.tagName.toLowerCase(), class: c.className, role: c.getAttribute('role') })),
                        shadow_root_children: Array.from(el.shadowRoot.children).slice(0, 8).map(c => ({ tag: c.tagName.toLowerCase(), class: c.className })),
                    };
                }
                scan(el.shadowRoot, path + ' >>> ' + el.tagName.toLowerCase());
                const shadowLinks = el.shadowRoot.querySelectorAll('a[href*="/jobs/view/"]');
                shadowLinks.forEach((a, i) => { if (i < 3) results.shadow_job_links.push({ path, chain: ancestorChain(a, 8) }); });
            }
        }
    }
    scan(document, 'document');

    results.body_text_length = document.body.innerText.length;
    results.body_job_word_count = (document.body.innerText.match(/job/gi) || []).length;
    return results;
}
"""


async def _save_dom_diagnosis(page: Page, name: str) -> None:
    """
    On a "no cards found" failure, dump every job-permalink element's ancestor
    chain, a shadow-DOM walk, and ARIA role/label matches (list items in the
    redesigned SPA may not use real <a href> navigation at all, so hrefs alone
    aren't reliable bait — accessibility roles/labels usually survive even
    when every classname is hashed by a CSS-modules build). This lets a future
    selector mismatch be root-caused from this artifact alone, without
    repeating a live, ad-hoc DOM investigation against the shared Chrome
    profile (risky — see linkedin_agent history: a one-off external Playwright
    session against the same profile caused a "Connection closed while
    reading from the driver" launch failure).
    """
    try:
        d = _ensure_debug_dir()
        report = await page.evaluate(_DOM_WALK_JS)
        (d / f"{name}.json").write_text(
            json.dumps({"url": page.url, "title": await page.title(), **report}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info(
            "linkedin_dom_diagnosis_saved",
            path=str(d / f"{name}.json"),
            light_dom_job_link_count=report.get("light_dom_job_link_count"),
            shadow_host_count=len(report.get("shadow_hosts", [])),
            li_count=report.get("li_count"),
            role_match_count=len(report.get("role_matches", [])),
            aria_job_label_count=len(report.get("aria_job_labels", [])),
        )
    except Exception as exc:
        logger.debug("linkedin_dom_diagnosis_failed", error=str(exc))


async def _retry(coro_fn, retries: int = 3, delay_s: float = 2.0):
    """Retry an async callable up to `retries` times on any exception."""
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return await coro_fn()
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                logger.debug("retry_attempt", attempt=attempt, error=str(exc))
                await asyncio.sleep(delay_s)
    raise last_exc  # type: ignore[misc]


def _group_recruiters_for_enrichment(
    jobs: list[LinkedInScrapedJob],
) -> dict[str, list[LinkedInScrapedJob]]:
    """Group harvested jobs by normalized recruiter LinkedIn URL, for the
    post-harvest recruiter contact-discovery pass (LinkedInAgent._enrich_recruiters).

    Skips any job whose linkedin_profile_url is missing, empty, or not a
    real "/in/" personal-profile URL — company/school pages or other stray
    links occasionally end up in this field, and aren't recruiter profiles
    to visit. Normalization matches
    app/services/recruiter_service.py::normalize_linkedin_url (lowercase,
    strip query params and trailing slash) so a recruiter who posted via
    multiple jobs with slightly different URL variants (tracking params,
    trailing slash) still groups into one entry.
    """
    groups: dict[str, list[LinkedInScrapedJob]] = {}
    for job in jobs:
        raw_url = (job.linkedin_profile_url or "").strip()
        if not raw_url or "/in/" not in raw_url.lower():
            continue
        norm_url = raw_url.split("?")[0].rstrip("/").lower()
        groups.setdefault(norm_url, []).append(job)
    return groups


# ══════════════════════════════════════════════════════════════════════════════
# LinkedIn Agent
# ══════════════════════════════════════════════════════════════════════════════

class LinkedInAgent:
    """
    LinkedIn job harvester using a persistent Chrome profile session.

    No login automation — the user logs in once via POST /linkedin-setup-session
    and the Chrome profile directory persists the session for all future runs.

    Instantiate fresh for each run.  PersistentBrowserManager is created and
    destroyed inside harvest().
    """

    # Safety valve: LinkedIn sometimes serves a build of the job-detail page
    # with every CSS class replaced by an opaque hash (anti-scraping
    # countermeasure) — none of the selectors in _Sel match it, even though
    # the page is fully authenticated and the description text is right
    # there in the DOM. When that happens we fall back to an LLM extraction
    # over the page's visible text instead of guessing new selectors. Capped
    # per-run so a fully-flagged session can't run up unbounded LLM cost.
    _LLM_FALLBACK_MAX_CALLS_PER_RUN     = 500
    _LLM_FALLBACK_TEXT_MAX_CHARS        = 16_000
    _LLM_FALLBACK_CARD_TEXT_MAX_CHARS   = 2_000

    # Recruiter contact-discovery post-harvest pass (see _enrich_recruiters) —
    # separate concern from the job-extraction LLM fallback above but shares
    # its call counter/cap (_llm_fallback_calls / _LLM_FALLBACK_MAX_CALLS_PER_RUN).
    _RECRUITER_CONTACT_SCRAPE_CAP = 50

    def __init__(self, llm_service: "LLMService | None" = None) -> None:
        self._llm_service        = llm_service
        self._llm_fallback_calls = 0
        # Fixed at construction (one LinkedInAgent per harvest run, per this
        # class's docstring) rather than re-read per job, so every relative
        # date ("3 days ago") on a multi-hour run resolves against the same
        # reference point instead of drifting as the run progresses.
        self._harvest_started_at = datetime.now(timezone.utc)

    def _get_llm_service(self) -> "LLMService":
        """Lazily build an LLMService from Settings if one wasn't injected —
        keeps existing no-arg `LinkedInAgent()` call sites working unchanged."""
        if self._llm_service is None:
            from app.config import get_settings
            from app.services.llm_service import LLMService
            self._llm_service = LLMService(get_settings())
        return self._llm_service

    def get_token_usage(self) -> dict:
        """Cumulative Claude/Ollama token usage from the LLM fallback calls
        made during this agent's lifetime (one LinkedInAgent per harvest run).
        Returns a zeroed summary if the LLM fallback was never triggered —
        i.e. every description was recovered via direct DOM selectors."""
        if self._llm_service is None:
            from app.services.llm_service import empty_usage_summary
            return empty_usage_summary()
        return self._llm_service.get_usage_summary()

    def get_llm_call_log(self) -> list[dict]:
        """Per-call audit log (provider, model, prompt/response, tokens,
        latency, success/error) from the LLM fallback calls made during this
        agent's lifetime. Empty if the fallback was never triggered."""
        if self._llm_service is None:
            return []
        return self._llm_service.get_call_log()

    # ── Public API ─────────────────────────────────────────────────────────────

    async def harvest(
        self,
        filters:  FiltersConfig,
        headless: bool = False,
        slow_mo:  int  = 0,
    ) -> list[LinkedInScrapedJob]:
        """
        Open LinkedIn Jobs with the persistent Chrome profile and harvest
        jobs matching filters.  Returns [] if the profile is not authenticated
        (user must call POST /linkedin-setup-session first).
        """
        from app.services.config_service import ConfigService
        chrome_profile = ConfigService().load().browser.chrome_profile

        logger.info(
            "config_loaded",
            source              = "linkedin",
            keyword             = filters.keyword,
            location            = filters.location,
            job_type            = filters.job_type,
            work_mode           = filters.work_mode,
            domain              = getattr(filters, "domain", "Any"),
            search_window_hours = filters.search_window_hours,
            max_jobs            = filters.max_jobs,
            chrome_profile      = chrome_profile,
        )
        logger.info(
            "linkedin_agent_started",
            keyword        = filters.keyword,
            location       = filters.location,
            job_type       = filters.job_type,
            work_mode      = filters.work_mode,
            max_jobs       = filters.max_jobs,
            chrome_profile = chrome_profile,
        )
        # Prefer session JSON (portable, always up to date after /linkedin-setup-session).
        # Fall back to profile directory only when no session file exists.
        from app.scrapers.browser_manager import BrowserManager
        from app.services.session_manager import SessionManager
        sm = SessionManager("linkedin")
        storage_state_arg = sm.storage_state_arg()

        if storage_state_arg:
            logger.info("linkedin_using_session_file", session_file=storage_state_arg)
            browser_ctx = BrowserManager(
                headless      = headless,
                slow_mo       = slow_mo,
                storage_state = storage_state_arg,
            )
        else:
            logger.warning(
                "linkedin_no_session_file",
                hint="No data/sessions/linkedin_session.json found — falling back to Chrome profile. "
                     "Call POST /linkedin-setup-session to create the session file.",
            )
            browser_ctx = PersistentBrowserManager(
                profile_dir = chrome_profile,
                headless    = headless,
                slow_mo     = slow_mo,
            )

        try:
            async with browser_ctx as bm:
                page = await bm.new_page()
                jobs = await self._run(page, filters)
        except LinkedInLoginError:
            raise
        except Exception as exc:
            logger.exception("agent_failed", source="linkedin", error=str(exc))
            return []

        logger.info("linkedin_jobs_received", count=len(jobs))
        logger.info(
            "agent_completed",
            source   = "linkedin",
            total    = len(jobs),
            keyword  = filters.keyword,
            location = filters.location,
        )
        logger.info(
            "linkedin_harvest_completed",
            total    = len(jobs),
            keyword  = filters.keyword,
            location = filters.location,
        )
        return jobs

    # ── Internal flow ──────────────────────────────────────────────────────────

    async def _run(
        self,
        page: Page,
        f: FiltersConfig,
        wait_for_login: bool = False,
        on_status: StatusCallback | None = None,
    ) -> list[LinkedInScrapedJob]:
        """
        Navigate directly to LinkedIn Jobs search.

        wait_for_login  When True and the session turns out to be unauthenticated,
                        pause here and poll for a human to complete manual login
                        via the live browser view instead of failing immediately.
                        Only meaningful for manually-triggered runs — a scheduled/
                        unattended run should keep failing fast (default False).
        on_status       Optional async callback fired with human-readable progress
                        messages (e.g. "waiting for login…") — lets the caller
                        surface live status to JobTracker / the frontend.
        """
        search_url = self._build_search_url(f, start=0)
        logger.info("search_started", source="linkedin", keyword=f.keyword, location=f.location)
        logger.info("search_url_generated", source="linkedin", url=search_url)
        logger.info("linkedin_navigating_to_jobs", url=search_url)

        await _screenshot(page, "page_loaded")

        # Retry the first navigation: right after the browser starts, the
        # container's embedded DNS resolver can briefly fail (Chromium reports
        # this as net::ERR_NAME_NOT_RESOLVED), which would otherwise abort the
        # whole run with 0 jobs. A couple of retries with backoff rides out the
        # transient blip.
        try:
            await _retry(
                lambda: page.goto(search_url, wait_until="domcontentloaded", timeout=30_000),
                retries=3,
                delay_s=3.0,
            )
        except Exception as exc:
            logger.error("linkedin_navigation_failed", error=str(exc))
            return []

        await page.wait_for_timeout(3_000)

        await self._ensure_authenticated(page, search_url, wait_for_login, on_status)

        current_url = page.url
        page_title  = await page.title()
        logger.info("results_page_loaded", source="linkedin", url=current_url, title=page_title)
        logger.info("linkedin_session_active", url=current_url)
        await _screenshot(page, "02_after_search")
        jobs = await self._paginate_and_collect(page, f)
        logger.info("linkedin_jobs_returned", count=len(jobs))

        # Post-harvest recruiter contact-discovery pass — deliberately after
        # pagination/detail extraction is fully done, not inside per-card
        # parsing (which would visit a recruiter's profile once per job they
        # posted instead of once per recruiter). Best-effort: any failure
        # here must not affect the job list already collected above.
        try:
            await self._enrich_recruiters(page, jobs)
        except Exception as exc:
            logger.warning("recruiter_enrichment_pass_failed", error=str(exc))

        return jobs

    async def _ensure_authenticated(
        self,
        page: Page,
        search_url: str,
        wait_for_login: bool,
        on_status: StatusCallback | None,
    ) -> None:
        """
        Verify the current page is on an authenticated LinkedIn session.

        Raises LinkedInLoginError if authentication can't be established
        (immediately when wait_for_login=False, or after the manual-login
        wait times out when wait_for_login=True).
        """
        page_title  = await page.title()
        current_url = page.url
        logger.info("search_page_opened", source="linkedin", url=current_url, title=page_title)

        # Check for redirect to login — profile session missing or expired
        if any(p in current_url for p in _GATED_PATHS):
            await _screenshot(page, "linkedin_gated_redirect")
            logger.warning(
                "linkedin_not_authenticated",
                url  = current_url,
                hint = "Chrome profile has no LinkedIn session. "
                       "Call POST /linkedin-setup-session to log in.",
            )
            # Retry once: navigate to linkedin.com home first, then to job search
            logger.info("linkedin_auth_retry", attempt=1)
            try:
                await page.goto("https://www.linkedin.com/", wait_until="domcontentloaded", timeout=20_000)
                await page.wait_for_timeout(2_000)
                await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(3_000)
                current_url = page.url
                logger.info("search_page_opened", source="linkedin", url=current_url, title=await page.title())
            except Exception as exc:
                logger.error("linkedin_auth_retry_failed", error=str(exc))
                raise LinkedInLoginError(f"LinkedIn navigation failed during auth retry: {exc}") from exc

            if any(p in current_url for p in _GATED_PATHS):
                logger.error(
                    "linkedin_not_authenticated",
                    url  = current_url,
                    hint = "LinkedIn requires authentication. "
                           "Call POST /linkedin-setup-session to log in once.",
                )
                if not wait_for_login:
                    logger.info("linkedin_jobs_returned", count=0, reason="authwall")
                    raise LinkedInLoginError(f"LinkedIn redirected to a gated page: {current_url}")
                await self._wait_for_manual_login(page, search_url, on_status)
                return

        # The URL-shape check above is not sufficient on its own: LinkedIn lets
        # logged-out guests browse the search RESULTS listing without a hard
        # authwall redirect (only individual job-detail pages gate more
        # aggressively), so a retry can "succeed" by this check while still
        # running as an unauthenticated guest — silently violating the
        # "NEVER fall back to guest/anonymous mode" rule documented at the top
        # of this file. Verify the li_at session cookie directly instead, the
        # same way /linkedin-auth-status and /linkedin-setup-session already do.
        cookies = await page.context.cookies()
        if any(c["name"] == "li_at" for c in cookies):
            logger.debug("linkedin_authenticated", url=current_url, cookie_count=len(cookies))
            return

        await _screenshot(page, "linkedin_no_li_at_cookie")
        logger.error(
            "linkedin_not_authenticated",
            url  = current_url,
            hint = "No li_at session cookie — browsing as a logged-out guest. "
                   "Call POST /linkedin-setup-session to log in.",
        )
        if not wait_for_login:
            raise LinkedInLoginError(
                "LinkedIn session is not authenticated (no li_at cookie). "
                "Call POST /linkedin-setup-session to log in."
            )
        await self._wait_for_manual_login(page, search_url, on_status)

    async def _wait_for_manual_login(
        self,
        page: Page,
        search_url: str,
        on_status: StatusCallback | None,
    ) -> None:
        """
        Pause the harvest and poll for a human to complete LinkedIn login.

        The same non-headless browser session backs the "Watch Live Browser"
        view in the UI, so the user can click into it and type credentials
        while this loop is waiting. Raises LinkedInLoginError on timeout.
        """
        wait_minutes = _LOGIN_WAIT_TIMEOUT_S // 60
        if on_status:
            await on_status(
                f"LinkedIn requires login — open 'Watch Live Browser' and log in "
                f"(waiting up to {wait_minutes} minutes)…"
            )
        logger.info("linkedin_waiting_for_manual_login", timeout_s=_LOGIN_WAIT_TIMEOUT_S)

        try:
            await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=20_000)
        except Exception as exc:
            logger.debug("linkedin_login_page_nav_failed", error=str(exc))

        waited = 0
        while waited < _LOGIN_WAIT_TIMEOUT_S:
            await page.wait_for_timeout(_LOGIN_WAIT_POLL_S * 1_000)
            waited += _LOGIN_WAIT_POLL_S
            try:
                cookies = await page.context.cookies()
            except Exception:
                continue
            if any(c["name"] == "li_at" for c in cookies):
                logger.info("linkedin_manual_login_detected", waited_s=waited)
                if on_status:
                    await on_status("Login detected — resuming harvest…")
                try:
                    await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
                    await page.wait_for_timeout(3_000)
                except Exception as exc:
                    logger.error("linkedin_post_login_nav_failed", error=str(exc))
                    raise LinkedInLoginError(f"Post-login navigation failed: {exc}") from exc
                return

        logger.error("linkedin_manual_login_timeout", timeout_s=_LOGIN_WAIT_TIMEOUT_S)
        raise LinkedInLoginError(
            f"Timed out after {wait_minutes} minutes waiting for manual LinkedIn login."
        )

    async def _paginate_and_collect(
        self, page: Page, f: FiltersConfig
    ) -> list[LinkedInScrapedJob]:
        """
        Paginate through LinkedIn results using &start=0,25,50,…

        Stops when:
        • No cards found on a page (results exhausted)
        • Two consecutive pages yield zero new (non-duplicate) jobs
        • Safety cap: f.max_jobs (0 = unlimited, default 500)
        """
        all_jobs:    list[LinkedInScrapedJob] = []
        seen_urls:   set[str]                 = set()
        page_num:    int                      = 0
        batch_size:  int                      = 25
        safety_cap:  int                      = f.max_jobs if f.max_jobs > 0 else 5_000
        empty_pages: int                      = 0

        test_cap = _test_mode_max_jobs()
        if test_cap is not None:
            logger.warning(
                "linkedin_test_mode_max_jobs_active",
                test_cap=test_cap, configured_max_jobs=f.max_jobs,
                hint="LINKEDIN_TEST_MAX_JOBS is set in .env — remove it to harvest normally.",
            )
            safety_cap = min(safety_cap, test_cap)

        logger.info(
            "linkedin_search_started",
            keyword   = f.keyword,
            location  = f.location,
            job_type  = f.job_type,
            work_mode = f.work_mode,
            max_jobs  = f.max_jobs,
        )
        logger.info("linkedin_pagination_started", safety_cap=safety_cap)

        while len(all_jobs) < safety_cap:
            start      = page_num * batch_size
            search_url = self._build_search_url(f, start=start)
            logger.info("linkedin_page_start", page=page_num + 1, start=start, collected=len(all_jobs))

            # Navigate
            async def _goto(url: str = search_url) -> None:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            try:
                await _retry(_goto)
            except Exception as exc:
                if page_num == 0:
                    await _screenshot(page, "linkedin_error")
                    await _save_html(page, "linkedin_error")
                    raise LinkedInLoginError(f"LinkedIn navigation failed: {exc}") from exc
                logger.warning("linkedin_page_nav_failed", page=page_num + 1, error=str(exc))
                break

            try:
                await _delay(page, 2_000, 3_000)
                self._check_blocked(page.url)
                await self._dismiss_overlays(page)

                if page_num == 0:
                    await _screenshot(page, "linkedin_jobs_page")

                logger.info("waiting_for_results", source="linkedin", page=page_num + 1, url=page.url)
                try:
                    await page.wait_for_load_state("networkidle", timeout=20_000)
                except Exception:
                    await _delay(page, 2_000, 3_000)

                page_title_now = await page.title()
                logger.info(
                    "results_page_loaded",
                    source = "linkedin",
                    page   = page_num + 1,
                    url    = page.url,
                    title  = page_title_now,
                )
                self._check_blocked(page.url)
                await self._dismiss_overlays(page)

                if page_num == 0:
                    logger.info("linkedin_jobs_page_opened", url=page.url)
                    logger.info("linkedin_results_page_loaded", url=page.url)

                await self._scroll_results(page)

                await _screenshot(page, f"linkedin_page_{page_num + 1:02d}_results")
            except Exception as exc:
                # Any unexpected failure while preparing this page (blocked,
                # overlay dismissal, scrolling, …) must not discard jobs already
                # collected from earlier pages — stop paginating and return them.
                logger.warning("linkedin_page_prepare_failed", page=page_num + 1, error=str(exc))
                break

            remaining = safety_cap - len(all_jobs)
            try:
                page_jobs = await self._extract_cards(page, remaining, seen_urls)
            except Exception as exc:
                # A page-wide extraction failure must not discard jobs already
                # collected from earlier pages — stop paginating and return them.
                logger.warning("linkedin_page_extract_failed", page=page_num + 1, error=str(exc))
                break

            if not page_jobs:
                empty_pages += 1
                logger.info("linkedin_page_empty", page=page_num + 1, consecutive_empty=empty_pages)
                if empty_pages >= 2:
                    logger.info("next_page_not_found", source="linkedin", page=page_num + 1, reason="consecutive_empty_pages")
                    break
            else:
                empty_pages = 0
                new_jobs: list[LinkedInScrapedJob] = []
                for j in page_jobs:
                    url = (j.job_url or "").split("?")[0].rstrip("/").lower()
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        all_jobs.append(j)
                        new_jobs.append(j)
                logger.info("next_page_found", source="linkedin", page=page_num + 1, jobs_this_page=len(page_jobs))

                # No domain-match early-stop: the domain filter is now applied by
                # LinkedIn's own native job-function filter (f_F, see
                # _build_search_url), so LinkedIn's filtered result set is the
                # source of truth. Pagination stops only on result exhaustion
                # (empty pages) or the max_jobs safety cap — re-classifying cards
                # here with the substring domain matcher would diverge from what
                # LinkedIn actually returned.

            logger.info("linkedin_page_done", page=page_num + 1, page_new=len(page_jobs), total=len(all_jobs))
            logger.info("page_processed", source="linkedin", page=page_num + 1, jobs_this_page=len(page_jobs), total_collected=len(all_jobs))
            page_num += 1
            await _delay(page, 1_500, 2_500)   # polite inter-page delay

        logger.info("pagination_completed", source="linkedin", pages=page_num, total=len(all_jobs))
        logger.info("linkedin_pagination_complete", pages=page_num, total=len(all_jobs))

        # ── Checkpoint 1: cumulative raw jobs ─────────────────────────────────
        logger.info("linkedin_jobs_extracted", count=len(all_jobs), pages_scraped=page_num)
        try:
            import json as _json_p
            _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            raw_dump = [
                {
                    "job_title": j.job_title, "company": j.company,
                    "location": j.location, "job_url": j.job_url,
                    "posted_date": j.posted_date,
                    "job_poster_name": j.job_poster_name,
                    "linkedin_profile_url": j.linkedin_profile_url,
                }
                for j in all_jobs
            ]
            (_DEBUG_DIR / "linkedin_raw_jobs.json").write_text(
                _json_p.dumps({"stage": "pagination_complete", "count": len(all_jobs), "jobs": raw_dump},
                              indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info("linkedin_raw_jobs_saved", path=str(_DEBUG_DIR / "linkedin_raw_jobs.json"), count=len(all_jobs))
        except Exception as _exc:
            logger.debug("linkedin_raw_jobs_save_failed", error=str(_exc))

        return all_jobs

    # ── Recruiter contact-discovery (post-harvest) ─────────────────────────────

    async def _enrich_recruiters(self, page: Page, jobs: list[LinkedInScrapedJob]) -> None:
        """
        Post-harvest recruiter contact-discovery pass.

        RecruiterORM (app/models/recruiter.py) is the single source of truth
        for recruiter identity/contact info — jobs are grouped by normalized
        recruiter LinkedIn URL (see _group_recruiters_for_enrichment), one
        RecruiterORM row is found-or-created per unique URL, and each
        profile is visited at most once (across runs, not just this one —
        a recruiter already fully enriched from a previous run is skipped
        entirely) using the SAME authenticated page's browser context this
        harvest just used, in a fresh tab that's always closed.

        Capped at _RECRUITER_CONTACT_SCRAPE_CAP *profile visits* per run
        (identity resolution/DB linking for the rest still happens — only
        the expensive browser visit is capped). Every recruiter is
        individually try/excepted: a DB outage, LinkedIn nav failure,
        Contact Info miss, or LLM error affects only that one recruiter,
        never the harvest's already-collected job list (the caller in _run
        also wraps this whole method for the same reason).
        """
        groups = _group_recruiters_for_enrichment(jobs)
        if not groups:
            return

        from app.config import get_settings
        from app.core.dependencies import get_session_factory
        from app.agents.prospect_intelligence_agent import _extract_linkedin_contact_info, _infer_department
        from app.services.recruiter_service import link_recruiter_jobs_by_url, save_enrichment, upsert_recruiter

        session_factory = get_session_factory(get_settings())
        visited = 0

        logger.info(
            "recruiter_enrichment_pass_started",
            unique_recruiters=len(groups), cap=self._RECRUITER_CONTACT_SCRAPE_CAP,
        )

        for norm_url, group_jobs in groups.items():
            sample       = group_jobs[0]
            person_name  = (sample.job_poster_name or "").strip()
            company_name = (sample.job_poster_company or sample.company or "").strip()
            if not person_name:
                continue

            try:
                async with session_factory() as db:
                    recruiter = await upsert_recruiter(
                        db,
                        person_name          = person_name,
                        company_name         = company_name,
                        designation          = sample.job_poster_designation or "",
                        linkedin_profile_url = norm_url,
                        harvest_source       = "LinkedIn",
                    )
                    if recruiter is None:
                        await db.commit()
                        continue
                    recruiter_id  = recruiter.id
                    already_email = recruiter.email_status in ("VERIFIED", "PUBLIC")
                    already_phone = recruiter.phone_status in ("VERIFIED", "PUBLIC")
                    await db.commit()
            except Exception as exc:
                logger.warning("recruiter_upsert_failed", url=norm_url, error=str(exc))
                continue

            if already_email and already_phone:
                # Already fully enriched by a previous run — the "visit
                # each unique recruiter profile only once" rule extends
                # across runs, so skip the visit but still backfill any
                # ScrapedJobORM rows from *this* run that reference them.
                try:
                    async with session_factory() as db:
                        await link_recruiter_jobs_by_url(db, recruiter_id, norm_url)
                        await db.commit()
                except Exception as exc:
                    logger.warning("recruiter_job_link_failed", url=norm_url, error=str(exc))
                continue

            if visited >= self._RECRUITER_CONTACT_SCRAPE_CAP:
                logger.info(
                    "recruiter_contact_scrape_cap_reached",
                    cap=self._RECRUITER_CONTACT_SCRAPE_CAP, unique_recruiters=len(groups),
                )
                break

            visited += 1
            contact_page: Page | None = None
            contact_info: dict = {"email": "", "phone": "", "headline": "", "location": ""}
            try:
                contact_page = await page.context.new_page()
                contact_info = await _extract_linkedin_contact_info(
                    contact_page, norm_url, llm_service=self._get_llm_service(),
                )

                if not contact_info.get("email") and not contact_info.get("phone"):
                    llm_contact = await self._llm_fallback_extract_contact(contact_page, norm_url)
                    if llm_contact.get("email"):
                        contact_info["email"] = llm_contact["email"]
                    if llm_contact.get("phone"):
                        contact_info["phone"] = llm_contact["phone"]
            except Exception as exc:
                logger.warning("recruiter_contact_visit_failed", url=norm_url, error=str(exc))
            finally:
                if contact_page:
                    try:
                        await contact_page.close()
                    except Exception:
                        pass

            found_email = bool(contact_info.get("email"))
            found_phone = bool(contact_info.get("phone"))

            try:
                async with session_factory() as db:
                    await save_enrichment(
                        db, recruiter_id,
                        official_email_id = contact_info.get("email", ""),
                        email_status      = "PUBLIC" if found_email else "NOT_FOUND",
                        contact_number    = contact_info.get("phone", ""),
                        phone_status      = "PUBLIC" if found_phone else "NOT_FOUND",
                        linkedin_headline = contact_info.get("headline", ""),
                        location          = contact_info.get("location", ""),
                        department        = _infer_department(
                            sample.job_poster_designation or "", contact_info.get("headline", ""),
                        ),
                        verified = found_email or found_phone,
                    )
                    await link_recruiter_jobs_by_url(db, recruiter_id, norm_url)
                    await db.commit()
            except Exception as exc:
                logger.warning("recruiter_enrichment_save_failed", url=norm_url, error=str(exc))

            logger.info(
                "recruiter_enriched",
                url=norm_url, person=person_name,
                found_email=found_email, found_phone=found_phone,
            )

        logger.info(
            "recruiter_enrichment_pass_complete",
            unique_recruiters=len(groups), profiles_visited=visited,
        )

    async def _llm_fallback_extract_contact(self, page: Page, profile_url: str) -> dict:
        """
        Last-resort contact extraction for one recruiter's LinkedIn profile —
        used only when the Contact Info modal + full-page-text regex scan
        (_extract_linkedin_contact_info) found neither an email nor a phone.

        Shares this agent's job-extraction LLM call counter/cap
        (_llm_fallback_calls / _LLM_FALLBACK_MAX_CALLS_PER_RUN) rather than a
        separate recruiter-specific budget, per design — recruiter contact
        LLM calls and job-detail LLM calls draw from the same per-run pool.
        """
        if self._llm_fallback_calls >= self._LLM_FALLBACK_MAX_CALLS_PER_RUN:
            logger.warning("recruiter_llm_fallback_cap_reached", url=profile_url)
            return {}

        try:
            html = await page.content()
        except Exception as exc:
            logger.debug("recruiter_llm_fallback_text_read_failed", url=profile_url, error=str(exc))
            return {}

        # Keep ONLY the profile owner's own region (top card + contact info +
        # About). Cut the text at the Activity section so the LLM never sees the
        # feed of reposts/comments — those belong to other members and would
        # otherwise leak their emails/phones in as this recruiter's contact.
        text = _trim_to_relevant(
            _cut_profile_text_before_activity(_html_to_text(html)),
            self._LLM_FALLBACK_TEXT_MAX_CHARS,
        )
        if len(text) < 50:
            return {}

        self._llm_fallback_calls += 1
        logger.info("recruiter_llm_fallback_started", url=profile_url, call_number=self._llm_fallback_calls)

        schema_description = (
            "{\"email\": str or null (ONLY if an email address is written "
            "verbatim in the text — never guess or construct one; null "
            "otherwise), \"phone\": str or null (ONLY if a phone number is "
            "written verbatim in the text — never guess or construct one; "
            "null otherwise)}"
        )
        content = f"---LINKEDIN PROFILE PAGE---\n{text}\n\n---EXTRACT JSON---\n{schema_description}"

        try:
            extracted = await self._get_llm_service().extract_json(
                content=content,
                schema_description=schema_description,
                system=(
                    "You are extracting only explicitly-stated contact details "
                    "from a LinkedIn profile page's text content. Never "
                    "fabricate, guess, or construct an email or phone number "
                    "that is not literally present in the text. Return only "
                    "the fields in the schema, no commentary."
                ),
                debug_dir=_DEBUG_DIR,
                job_url=profile_url,
            )
        except LLMUnavailableError:
            # LLM provider is down — abort the run (see _llm_fallback_extract).
            raise
        except Exception as exc:
            logger.warning("recruiter_llm_fallback_failed", url=profile_url, error=str(exc))
            return {}

        result: dict = {}
        if extracted.get("email"):
            result["email"] = str(extracted["email"]).strip()
        if extracted.get("phone"):
            result["phone"] = str(extracted["phone"]).strip()
        return result

    # ── Search URL builder ─────────────────────────────────────────────────────

    @staticmethod
    def _compose_keyword_query(keyword: str, domain: str) -> str:
        """The LinkedIn `keywords=` value.

        A user-typed keyword always wins. Otherwise the chosen domain label is
        placed into the search box exactly as-is — e.g. "Data Engineering",
        "AI/ML", "Cyber Security", "IT" — so LinkedIn searches for that role
        directly (no boolean OR-set, no native function filter). This makes
        LinkedIn's own filtered results the source of truth. "Any" and "Non-IT"
        have no meaningful search term, so they contribute nothing (LinkedIn
        returns its unfiltered result set)."""
        keyword = (keyword or "").strip()
        if keyword:
            return keyword
        if domain in ("", "Any", "Non-IT"):
            return ""
        return domain

    @staticmethod
    def _build_search_url(f: FiltersConfig, start: int = 0) -> str:
        # The domain is placed into the `keywords=` search box as-is (see
        # _compose_keyword_query) — no boolean OR-set, no native function filter.
        # LinkedIn's own filtered results are the source of truth.
        keyword_query = LinkedInAgent._compose_keyword_query(f.keyword, f.domain)
        params: list[str] = [
            f"keywords={quote_plus(keyword_query)}",
        ]
        # location is optional — only constrain the search when one is provided.
        if f.location:
            params.append(f"location={quote_plus(f.location)}")
        params.append("sortBy=DD")
        if wt := _WORK_MODE_MAP.get(f.work_mode, ""):
            params.append(f"f_WT={wt}")
        if jt := _JOB_TYPE_MAP.get(f.job_type, ""):
            params.append(f"f_JT={jt}")
        # The time window is applied ONLY through LinkedIn's native date filter
        # (f_TPR) — never mixed into `keywords`. Keep it that way: putting "past
        # 24 hours" into the keyword would make LinkedIn search job TEXT for those
        # words instead of restricting by post date.
        if tpr := _DATE_MAP.get(f.search_window_hours, ""):
            params.append(f"f_TPR={tpr}")
        if start > 0:
            params.append(f"start={start}")
        return _LINKEDIN_SEARCH_URL + "&".join(params)

    # ── Block detection ────────────────────────────────────────────────────────

    @staticmethod
    def _check_blocked(url: str) -> None:
        for pat in _GATED_PATHS:
            if pat in url:
                raise LinkedInLoginError(f"LinkedIn redirected to a gated page: {url}")

    # ── Overlay dismissal ──────────────────────────────────────────────────────

    async def _dismiss_overlays(self, page: Page) -> None:
        for sel in _Sel.COOKIE:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1_200):
                    await el.click()
                    await _delay(page, 400, 700)
                    logger.debug("linkedin_cookie_banner_dismissed", selector=sel)
                    break
            except Exception:
                continue
        for sel in _Sel.MODAL_DISMISS:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1_000):
                    await el.click()
                    await _delay(page, 400, 700)
                    logger.debug("linkedin_modal_dismissed", selector=sel)
                    break
            except Exception:
                continue
        try:
            if await page.locator('div[role="dialog"]').first.is_visible(timeout=600):
                await page.keyboard.press("Escape")
                await _delay(page, 300, 600)
                logger.debug("linkedin_dialog_escaped")
        except Exception:
            pass

    # ── Scrolling ──────────────────────────────────────────────────────────────

    async def _scroll_results(self, page: Page) -> None:
        container     = None
        matched_sel   = None
        for sel in _Sel.CONTAINER:
            try:
                el = await page.query_selector(sel)
                if el:
                    container   = el
                    matched_sel = sel
                    logger.info("jobs_container_found", source="linkedin", selector=sel)
                    break
            except Exception:
                continue

        if not container:
            logger.info("jobs_container_not_found", source="linkedin", selectors_tried=_Sel.CONTAINER)

        if container:
            prev_h = -1
            iterations = 0
            for _ in range(25):
                h = await container.evaluate("el => el.scrollHeight")
                if h == prev_h:
                    break
                prev_h = h
                await container.evaluate("el => el.scrollTo(0, el.scrollHeight)")
                await _delay(page, 400, 800)
                iterations += 1
            logger.debug("linkedin_scroll_done", mode="container", selector=matched_sel, iterations=iterations, final_height=prev_h)
        else:
            logger.debug("linkedin_no_container_scrolling_window")
            prev_h = -1
            iterations = 0
            for _ in range(15):
                h = await page.evaluate("document.body.scrollHeight")
                if h == prev_h:
                    break
                prev_h = h
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await _delay(page, 600, 1_000)
                iterations += 1
            logger.debug("linkedin_scroll_done", mode="window", iterations=iterations, final_height=prev_h)

    # ── Card extraction ────────────────────────────────────────────────────────

    async def _extract_cards(
        self,
        page:      Page,
        remaining: int,
        seen_urls: set[str] | None = None,
    ) -> list[LinkedInScrapedJob]:
        """Extract job cards from the current page. Returns only new (non-duplicate) jobs."""
        if seen_urls is None:
            seen_urls = set()

        matched_card_sel = None
        for sel in _Sel.CARD:
            try:
                await page.wait_for_selector(sel, timeout=5_000)
                matched_card_sel = sel
                logger.info("linkedin_card_selector_matched", selector=sel)
                break
            except Exception:
                logger.info("linkedin_selector_timeout", selector=sel)
                continue

        raw: list[ElementHandle] = []
        for sel in _Sel.CARD:
            found = await page.query_selector_all(sel)
            cnt   = len(found)
            logger.info("linkedin_selector_tried", selector=sel, count=cnt)
            if found:
                raw = found
                logger.info("job_cards_found_count", source="linkedin", selector=sel, count=cnt)
                logger.info("linkedin_cards_found", selector=sel, count=cnt)
                logger.info("linkedin_jobs_found", count=cnt)
                break

        if not raw:
            await _screenshot(page, "linkedin_no_cards")
            await _save_html(page, "linkedin_no_cards")
            await _save_dom_diagnosis(page, "linkedin_dom_diagnosis")
            # Distinguish "selectors are stale" from "not actually authenticated
            # / wrong template" without needing a manual DOM investigation next
            # time: a real authenticated results page always has some element
            # linking to a job permalink, even if its wrapper markup changed.
            try:
                job_link_count = len(await page.query_selector_all("a[href*='/jobs/view/']"))
                has_li_at      = any(c["name"] == "li_at" for c in await page.context.cookies())
            except Exception:
                job_link_count, has_li_at = -1, None
            logger.warning(
                "linkedin_job_cards_not_found",
                source="linkedin", selectors_tried=_Sel.CARD,
                url=page.url, job_link_count=job_link_count, has_li_at_cookie=has_li_at,
                hint=(
                    "job_link_count>0 with cards_not_found means LinkedIn's card "
                    "markup/classnames changed — update _Sel.CONTAINER/_Sel.CARD. "
                    "job_link_count==0 or has_li_at_cookie==False means the session "
                    "isn't really on the authenticated results page (logged out or "
                    "served the guest template) — re-authenticate the Chrome profile."
                ),
            )
            logger.warning("linkedin_no_cards_found")
            logger.info("linkedin_cards_found", count=0)
            return []

        await _screenshot(page, "cards_found")
        logger.info("linkedin_cards_found", count=len(raw), selector=matched_card_sel)
        return await self._parse_cards_with_detail(page, raw, remaining, seen_urls)

    async def _parse_cards_with_detail(
        self,
        page:      Page,
        raw:       list[ElementHandle],
        remaining: int,
        seen_urls: set[str],
    ) -> list[LinkedInScrapedJob]:
        import json as _json
        jobs: list[LinkedInScrapedJob] = []

        # ── Phase A: enumerate ALL cards on this page up front ─────────────────
        # Read every card's list-view fields (title + stable /jobs/view/<id> URL)
        # into a plain list BEFORE opening any detail page. This captures the full
        # page's cards in order, and — because no ElementHandle is touched after
        # this phase — removes any stale-locator risk from the detail navigation
        # that follows. Dedup here against jobs already collected on earlier pages
        # (seen_urls) and against duplicates within this page (page_seen).
        enumerated: list[dict] = []
        page_seen: set[str] = set()
        for idx, card_el in enumerate(raw):
            # Stop reading cards once we've gathered `remaining` of them — no
            # point parsing the rest of the page when the cap (e.g.
            # LINKEDIN_TEST_MAX_JOBS) will only let Phase B fetch that many.
            # For a real harvest `remaining` is far larger than a page's card
            # count, so this simply enumerates the whole page.
            if len(enumerated) >= remaining:
                break
            try:
                list_data = await self._parse_card_list_view(card_el)
            except Exception as exc:
                logger.warning("linkedin_card_list_view_failed", index=idx, error=str(exc))
                continue
            if not list_data or not list_data.get("url"):
                continue
            norm_url = list_data["url"].split("?")[0].rstrip("/").lower()
            if norm_url and (norm_url in seen_urls or norm_url in page_seen):
                continue
            if norm_url:
                page_seen.add(norm_url)
            list_data["idx"] = idx
            enumerated.append(list_data)

        logger.info("linkedin_cards_enumerated", count=len(enumerated), page_cards=len(raw))

        # ── Phase B: fetch each enumerated card's detail (new tab, by URL) ─────
        for list_data in enumerated:
            if len(jobs) >= remaining:
                break

            idx = list_data.get("idx", 0)
            url = list_data["url"]
            try:
                # ── Fetch job detail by navigating directly to its own URL ─────────
                # (not by clicking the card — see _fetch_job_detail for why)
                detail_data = await self._fetch_job_detail(
                    page, url, idx,
                    card_text  = list_data.get("card_text", ""),
                    card_links = list_data.get("card_links", []),
                )

                # ── Merge list + detail data ──────────────────────────────────────
                title    = detail_data.get("title") or list_data.get("title") or ""
                company  = detail_data.get("company") or list_data.get("company") or "Unknown Company"
                location = detail_data.get("location") or list_data.get("location") or "Unknown Location"

                if not title:
                    continue

                work_mode = _infer_work_mode(
                    detail_data.get("emp_type", "") + " " + location
                )

                # Description: formatted plain text (for reports) + rich HTML
                # (for the UI). HTML is resolved first-non-empty-wins: the DOM
                # container capture (truest, but LinkedIn's obfuscated auth page
                # rarely matches), else the LLM's own clean HTML (sanitized),
                # else a deterministic conversion of the formatted plain text —
                # so the UI always gets formatted HTML when a description exists.
                formatted_desc = format_job_description(detail_data.get("description", ""))
                description_html = (
                    detail_data.get("job_description_html", "")
                    or sanitize_description_html(detail_data.get("description_html", ""))
                    or description_text_to_html(formatted_desc)
                )

                job = LinkedInScrapedJob(
                    job_title               = _clean(title),
                    company                 = _clean(company),
                    location                = _clean(location),
                    salary                  = _clean(detail_data.get("salary", "")) or "Not Disclosed",
                    experience              = "Not Specified",
                    posted_date             = _format_posted(
                        detail_data.get("posted") or list_data.get("posted") or ""
                    ),
                    job_url                 = url,
                    job_description         = formatted_desc,
                    job_description_html    = description_html,
                    skills                  = detail_data.get("skills", []),
                    work_mode               = work_mode,
                    company_url             = detail_data.get("company_url", ""),
                    employment_type         = _clean(detail_data.get("emp_type", "")),
                    industry_hint           = _clean(detail_data.get("job_insights", "")),
                    source                  = "LinkedIn",
                    job_poster_name         = detail_data.get("recruiter_name") or None,
                    job_poster_designation  = detail_data.get("recruiter_title") or None,
                    linkedin_profile_url    = detail_data.get("recruiter_url") or None,
                    job_poster_company      = detail_data.get("recruiter_company") or None,
                    job_poster_email        = detail_data.get("recruiter_email") or None,
                    job_poster_phone        = detail_data.get("recruiter_phone") or None,
                )
                jobs.append(job)
                logger.info(
                    "linkedin_job_extracted",
                    source  = "linkedin",
                    index   = idx,
                    title   = title,
                    company = company,
                    url     = url,
                    recruiter = detail_data.get("recruiter_name"),
                )
                logger.info("job_card_extracted", source="linkedin", index=idx, title=title, company=company, url=url)
            except LLMUnavailableError:
                # The extraction LLM is down — stop the whole page/run instead of
                # churning through the remaining cards against a dead provider.
                raise
            except Exception as exc:
                # One bad card (timeout, stale element, transient nav failure) must
                # not discard every job already extracted from this page/run.
                logger.warning("linkedin_card_extract_failed", index=idx, error=str(exc))
                continue

        # Save raw jobs JSON artifact
        try:
            _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            raw_data = [
                {
                    "title": j.job_title, "company": j.company, "location": j.location,
                    "url": j.job_url, "posted_date": j.posted_date,
                    "job_poster_name": j.job_poster_name,
                    "linkedin_profile_url": j.linkedin_profile_url,
                }
                for j in jobs
            ]
            (_DEBUG_DIR / "linkedin_raw_jobs.json").write_text(
                _json.dumps(raw_data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            logger.info("linkedin_raw_jobs_saved", path=str(_DEBUG_DIR / "linkedin_raw_jobs.json"), count=len(jobs))
        except Exception as exc:
            logger.debug("linkedin_raw_jobs_save_failed", error=str(exc))

        logger.info("linkedin_jobs_combined", count=len(jobs))
        logger.info("linkedin_extraction_complete", count=len(jobs))
        return jobs

    async def _parse_card_list_view(self, el: ElementHandle) -> dict | None:
        """Extract the minimal fields visible in the list-view card."""
        try:
            title = _clean(await _first_text(el, _Sel.TITLE))
            if not title:
                # Fallback: the current split-view card renders as a bare
                # <div role="button" componentkey="job-card-component-ref-…">
                # with no matching text selector, but it does carry a sibling
                # "Dismiss <Title> job" button — its aria-label is the one
                # place the job title still appears verbatim.
                try:
                    dismiss_el = await el.query_selector("button[aria-label^='Dismiss ']")
                    if dismiss_el:
                        aria = (await dismiss_el.get_attribute("aria-label")) or ""
                        m = re.match(r"^Dismiss\s+(.+?)\s+job$", aria.strip())
                        if m:
                            title = _clean(m.group(1))
                except Exception:
                    pass

            if not title:
                # Log inner text snippet for debugging selector mismatches
                try:
                    snippet = (await el.inner_text())[:120].replace("\n", " ")
                    logger.debug("linkedin_card_no_title", snippet=snippet)
                except Exception:
                    pass
                return None

            href = await _first_attr(el, _Sel.LINK, "href")
            # Fallback: build URL from data-occludable-job-id attribute
            if not href:
                job_id = await el.get_attribute("data-occludable-job-id")
                if job_id:
                    href = f"https://www.linkedin.com/jobs/view/{job_id}/"
            # Fallback: build URL from the componentkey wiring attribute
            # (see _Sel.CARD) — "job-card-component-ref-<jobId>".
            if not href:
                component_key = await el.get_attribute("componentkey")
                if component_key and component_key.startswith("job-card-component-ref-"):
                    job_id = component_key.removeprefix("job-card-component-ref-")
                    if job_id:
                        href = f"https://www.linkedin.com/jobs/view/{job_id}/"
            clean_url = href.split("?")[0] if href else ""
            if clean_url.startswith("/"):
                clean_url = "https://www.linkedin.com" + clean_url

            # Grab the card's own visible text + any /in/ profile links it
            # contains — recruiter/poster info is sometimes only shown on the
            # search-results card and not repeated on the detail page. Kept
            # alongside the detail page's text for the LLM fallback extraction
            # (see LinkedInAgent._llm_fallback_extract).
            card_text  = ""
            card_links: list[dict] = []
            try:
                card_eval = await el.evaluate(
                    "(node) => ({"
                    "  text: node.innerText || '',"
                    "  links: Array.from(node.querySelectorAll(\"a[href*='/in/']\"))"
                    "    .slice(0, 10).map(a => ({text: (a.innerText||'').trim(), href: a.href}))"
                    "})"
                )
                card_text  = card_eval.get("text", "") if card_eval else ""
                card_links = card_eval.get("links", []) if card_eval else []
            except Exception as exc:
                logger.debug("linkedin_card_text_read_failed", error=str(exc))

            company  = _clean(await _first_text(el, _Sel.COMPANY))
            location = _clean(await _first_text(el, _Sel.LOCATION))
            logger.debug(
                "linkedin_card_list_view_parsed",
                title=title, company=company, location=location, url=clean_url,
            )
            return {
                "title":    title,
                "company":  company,
                "location": location,
                "posted":   (
                    await _first_attr(el, _Sel.POSTED, "datetime")
                    or await _first_text(el, _Sel.POSTED)
                ),
                "url":         clean_url,
                "card_text":   card_text,
                "card_links":  card_links,
            }
        except Exception as exc:
            logger.debug("linkedin_list_view_parse_error", error=str(exc))
            return None

    async def _fetch_job_detail(
        self,
        page: Page,
        url: str,
        idx: int,
        card_text:  str        = "",
        card_links: list[dict] | None = None,
    ) -> dict:
        """
        Open the job's own /jobs/view/<id>/ page in a separate tab and extract
        detail fields there.

        card_text / card_links  Visible text + /in/ profile links captured from
                                 this job's search-results card (see
                                 _parse_card_list_view). Forwarded to the LLM
                                 fallback so it has both pages' content when the
                                 detail page's own selectors fail to match.

        Deliberately does NOT click the card in the split-view search results
        list — that approach was found to fail 100% of the time in production
        (no detail panel ever matched within the wait timeout, for every job in
        the run), most likely due to click/render timing under Xvfb. Navigating
        directly to the job's own URL sidesteps that entirely — same technique
        already proven working in app/scrapers/linkedin_scraper.py.
        """
        detail: dict = {}
        detail_page: Page | None = None
        try:
            detail_page = await page.context.new_page()
            try:
                # A brand-new tab navigating cold (no Referer) gets served a
                # different page than a natural click-through would — LinkedIn
                # appears to render a plain SEO shell (no #job-details) or an
                # authwall for referrer-less navigation even on a valid,
                # authenticated session. Passing the search-results page's own
                # URL as the referer makes this look like what it actually is:
                # a click-through from that search.
                await detail_page.goto(
                    url, wait_until="domcontentloaded", timeout=25_000, referer=page.url,
                )
            except Exception as exc:
                logger.info("linkedin_detail_page_nav_failed", idx=idx, url=url, error=str(exc))
                return detail

            await _delay(detail_page, 1_500, 2_500)
            await self._dismiss_overlays(detail_page)
            logger.info(
                "job_opened", source="linkedin", index=idx,
                requested_url=url, landed_url=detail_page.url,
            )

            # Wait for the page to actually finish hydrating before touching
            # it further — domcontentloaded + the fixed delay above isn't a
            # guarantee on this client-rendered SPA. A capture taken too early
            # can land with only nav/footer chrome and no real job content at
            # all, even though the same page finishes rendering moments later
            # (confirmed live via a saved debug HTML/LLM-prompt pair). This
            # also ensures the "Show more" button below actually exists yet.
            stable_len = await _wait_for_page_text_stable(detail_page)
            logger.debug("linkedin_detail_page_text_stable", idx=idx, text_length=stable_len)

            # Expand truncated description ("Show more" / "… more") BEFORE the
            # text capture below — the LLM extraction a few lines down reads
            # the page's full HTML directly. The button selectors are matched
            # by visible text, not by (obfuscated) class names, so this works
            # regardless of LinkedIn's current markup. Doing this first is
            # what makes the extraction see the complete description instead
            # of the collapsed "…more" preview.
            for sel in _Sel.SHOW_MORE_BTN:
                try:
                    btn = detail_page.locator(sel).first
                    if await btn.is_visible(timeout=1_000):
                        await btn.click()
                        await _delay(detail_page, 400, 700)
                        break
                except Exception:
                    continue

            # Capture the description container's own inner HTML AFTER expanding
            # "Show more", preserving LinkedIn's rich formatting (headings,
            # bullet lists, bold, links). Sanitized to a safe display subset and
            # stored in job_description_html — separate from the LLM's plain-text
            # description so the JSON/Excel reports stay tag-free. Best-effort:
            # if no container matches, the UI falls back to the plain text.
            desc_html = ""
            for sel in _DESCRIPTION_SELECTORS:
                try:
                    el = await detail_page.query_selector(sel)
                    if el:
                        raw_html = await el.inner_html()
                        if raw_html and raw_html.strip():
                            desc_html = sanitize_description_html(raw_html)
                            if desc_html:
                                break
                except Exception:
                    continue

            # LinkedIn drifts/obfuscates its detail-page CSS classes constantly
            # (in production, hardcoded selectors matched 0 of the jobs in a
            # run — see linkedin_detail_panel_not_found history). Rather than
            # matching selectors at all, extract every field via the LLM from
            # the page's full HTML with tags/classes stripped down to text.
            llm_detail = await self._llm_fallback_extract(
                detail_page, idx, url, card_text=card_text, card_links=card_links,
            )
            detail.update(llm_detail)
            if desc_html:
                detail["job_description_html"] = desc_html

            if not detail.get("description"):
                logger.info("linkedin_description_still_empty", idx=idx, url=url)
                await _screenshot(detail_page, f"desc_empty_{idx:03d}")
                await _save_html(detail_page, f"desc_empty_{idx:03d}")

        except Exception as exc:
            logger.info("linkedin_detail_page_error", idx=idx, url=url, error=str(exc))
        finally:
            if detail_page:
                try:
                    await detail_page.close()
                except Exception:
                    pass

        return detail

    async def _llm_fallback_extract(
        self,
        page:       Page,
        idx:        int,
        url:        str,
        card_text:  str        = "",
        card_links: list[dict] | None = None,
    ) -> dict:
        """
        Extracts every detail-page field via the LLM instead of matching
        LinkedIn's CSS selectors, which drift/obfuscate constantly (in
        production, hardcoded selectors matched 0 of the jobs in a run — see
        linkedin_detail_panel_not_found history).

        Reads the page's full HTML (page.content()) and strips
        tags/scripts/styles/classes down to plain text via _html_to_text —
        this recovers text innerText would miss too, e.g. a "Meet the hiring
        team" card that's present in the DOM but CSS-hidden for a guest/
        unauthenticated session. Capped at _LLM_FALLBACK_TEXT_MAX_CHARS,
        combined with the search-results card's own visible text (recruiter/
        poster info is sometimes only shown there, not on the detail page),
        plus a de-duplicated list of `/in/` profile links gathered from
        *both* pages by href pattern (not by class, so it survives the same
        obfuscation) so the LLM can pick out a recruiter/poster URL that the
        page text alone wouldn't contain.

        Routed through LLMService.extract_json(), which itself picks Claude or
        a local Ollama model per EXTRACTION_LLM_MODEL — this method doesn't
        care which provider actually answers.
        """
        if self._llm_fallback_calls >= self._LLM_FALLBACK_MAX_CALLS_PER_RUN:
            logger.warning(
                "linkedin_llm_fallback_cap_reached",
                idx=idx, cap=self._LLM_FALLBACK_MAX_CALLS_PER_RUN,
            )
            return {}

        try:
            html = await page.content()
        except Exception as exc:
            logger.info("linkedin_llm_fallback_text_read_failed", idx=idx, error=str(exc))
            return {}

        detail_text = _trim_to_relevant(_html_to_text(html), self._LLM_FALLBACK_TEXT_MAX_CHARS)
        if len(detail_text) < 100:
            logger.info("linkedin_llm_fallback_text_too_short", idx=idx, chars=len(detail_text))
            return {}

        card_text_clean = _clean(card_text or "")[: self._LLM_FALLBACK_CARD_TEXT_MAX_CHARS]

        profile_links: list[dict] = list(card_links or [])
        try:
            detail_links = await page.eval_on_selector_all(
                "a[href*='/in/']",
                "els => els.slice(0, 15).map(e => ({text: e.innerText.trim(), href: e.href}))",
            )
            profile_links.extend(detail_links)
        except Exception as exc:
            logger.debug("linkedin_llm_fallback_links_failed", idx=idx, error=str(exc))

        seen_hrefs: set[str] = set()
        deduped_links: list[dict] = []
        for link in profile_links:
            href = link.get("href")
            if href and href not in seen_hrefs:
                seen_hrefs.add(href)
                deduped_links.append(link)
        profile_links = deduped_links[:20]

        company_links: list[dict] = []
        try:
            company_links = await page.eval_on_selector_all(
                "a[href*='/company/']",
                "els => els.slice(0, 10).map(e => ({text: e.innerText.trim(), href: e.href}))",
            )
        except Exception as exc:
            logger.debug("linkedin_llm_fallback_company_links_failed", idx=idx, error=str(exc))

        # Pre-resolve the posting date here in Python (card text first, then
        # detail text) instead of asking the LLM to do the "X days ago" →
        # YYYY-MM-DD arithmetic itself — see _resolve_posted_date.
        resolved_date = (
            _resolve_posted_date(card_text_clean, self._harvest_started_at)
            or _resolve_posted_date(detail_text, self._harvest_started_at)
        )

        self._llm_fallback_calls += 1
        logger.info(
            "linkedin_llm_fallback_started",
            idx=idx, url=url,
            detail_text_chars=len(detail_text), card_text_chars=len(card_text_clean),
            profile_link_candidates=len(profile_links),
            call_number=self._llm_fallback_calls,
            resolved_date=resolved_date,
        )

        schema_description = (
            "{\"title\": str (the job title; empty string if not present), "
            "\"company\": str (the hiring company's name; empty if not present), "
            "\"company_url\": str or null (pick the matching href from the "
            "candidate company links below by matching the name, null if no match), "
            "\"location\": str (city/region and Remote/Hybrid/On-site if stated; "
            "empty if not present), "
            "\"posted\": str or null (copy the pre-calculated posting date given "
            "above verbatim — do not calculate or derive it yourself; null if it "
            "was marked unavailable), "
            "\"description\": str (the COMPLETE job description / \"About the job\" "
            "text — include every paragraph and bullet point verbatim, do not "
            "summarize or shorten it; empty string if not present), "
            "\"description_html\": str (the SAME complete description formatted as "
            "clean, simple HTML using ONLY these tags: <p>, <ul>, <li>, <strong>, "
            "<em>, <h3>, <br> — preserve headings, paragraphs and bullet lists; no "
            "attributes, styles, classes, scripts, or any other tag; empty string "
            "if not present), "
            "\"employment_type\": str (e.g. Full-time, Contract; empty if not stated), "
            "\"salary\": str (empty if not disclosed), "
            "\"job_insights\": str (employment type/seniority/company size/industry "
            "bullets shown near the title, joined with \" | \"; empty if none), "
            "\"skills\": list[str] (empty list if none listed), "
            "\"recruiter_name\": str or null (name of the recruiter / hiring "
            "manager / job poster — check both the search-card text and the "
            "detail-page text, e.g. a \"Meet the hiring team\" section or a "
            "poster byline on the card; null if none mentioned), "
            "\"recruiter_title\": str or null (their job title/designation, null if unknown), "
            "\"recruiter_company\": str or null (the recruiter's own company, only if "
            "explicitly stated and different from the hiring company; null otherwise), "
            "\"recruiter_url\": str or null (pick the matching href from the "
            "candidate profile links below by matching the name, null if no match), "
            "\"recruiter_email\": str or null (ONLY if an email address is written "
            "verbatim in the text — never guess or construct one; null otherwise), "
            "\"recruiter_phone\": str or null (ONLY if a phone number is written "
            "verbatim in the text — never guess or construct one; null otherwise)}"
        )
        # Labelled sections, short context first (card/links/date) and the
        # long detail-page text last — the LLM sees the most important
        # signals before wading into a much longer blob, instead of that
        # blob burying them.
        content = (
            f"---CARD TEXT---\n{card_text_clean or '(none)'}\n\n"
            f"---COMPANY LINKS---\n{json.dumps(company_links, ensure_ascii=False)}\n\n"
            f"---PROFILE LINKS---\n{json.dumps(profile_links, ensure_ascii=False)}\n\n"
            f"---POSTING DATE (pre-calculated)---\n{resolved_date or 'unavailable'}\n\n"
            f"---JOB DETAIL PAGE---\n{detail_text}\n\n"
            f"---EXTRACT JSON---\n{schema_description}"
        )

        try:
            extracted = await self._get_llm_service().extract_json(
                content=content,
                schema_description=schema_description,
                system=(
                    "You are extracting structured job-posting data from the "
                    "text content of LinkedIn pages (a search-result card "
                    "and/or the job's detail page, with all HTML tags/classes "
                    "already stripped) — work from the text content only. "
                    "Prioritize returning the COMPLETE job description and any "
                    "recruiter contact details that are explicitly present. "
                    "For description_html, reproduce the same description as "
                    "clean HTML using ONLY <p>, <ul>, <li>, <strong>, <em>, "
                    "<h3>, <br> — never add attributes, classes, styles, or "
                    "scripts. Never fabricate an email, phone number, url, or "
                    "any other field that is not literally present in the text "
                    "or link list. Return only the fields in the schema, no "
                    "commentary."
                ),
                debug_dir=_DEBUG_DIR,
                job_url=url,
            )
        except LLMUnavailableError:
            # The extraction LLM itself is down — abort the whole run rather
            # than degrade every remaining job. Propagates up to run_all().
            raise
        except Exception as exc:
            logger.warning("linkedin_llm_fallback_failed", idx=idx, url=url, error=str(exc))
            return {}

        # The LLM sometimes matches the right company but returns an empty or
        # slightly-off company_url despite a clear candidate being in
        # company_links — resolve it directly from the links instead.
        if not extracted.get("company_url") and company_links and extracted.get("company"):
            fallback_url = _resolve_company_url_from_links(str(extracted["company"]), company_links)
            if fallback_url:
                extracted["company_url"] = fallback_url
                logger.debug(
                    "linkedin_company_url_resolved_via_fallback",
                    idx=idx, company=extracted["company"], url=fallback_url,
                )

        result: dict = {}
        if extracted.get("title"):
            result["title"] = str(extracted["title"]).strip()
        if extracted.get("company"):
            result["company"] = str(extracted["company"]).strip()
        if extracted.get("company_url"):
            result["company_url"] = str(extracted["company_url"]).strip()
        if extracted.get("location"):
            result["location"] = str(extracted["location"]).strip()
        if extracted.get("posted"):
            result["posted"] = str(extracted["posted"]).strip()
        if extracted.get("description"):
            result["description"] = str(extracted["description"]).strip()[:20_000]
        if extracted.get("description_html"):
            result["description_html"] = str(extracted["description_html"]).strip()[:40_000]
        if extracted.get("employment_type"):
            result["emp_type"] = str(extracted["employment_type"]).strip()
        if extracted.get("salary"):
            result["salary"] = str(extracted["salary"]).strip()
        if extracted.get("job_insights"):
            result["job_insights"] = str(extracted["job_insights"]).strip()
        if extracted.get("skills"):
            result["skills"] = [str(s).strip() for s in extracted["skills"] if str(s).strip()][:20]
        if extracted.get("recruiter_name"):
            result["recruiter_name"] = str(extracted["recruiter_name"]).strip()
        if extracted.get("recruiter_title"):
            result["recruiter_title"] = str(extracted["recruiter_title"]).strip()
        if extracted.get("recruiter_company"):
            result["recruiter_company"] = str(extracted["recruiter_company"]).strip()
        if extracted.get("recruiter_url"):
            result["recruiter_url"] = str(extracted["recruiter_url"]).strip()
        if extracted.get("recruiter_email"):
            result["recruiter_email"] = str(extracted["recruiter_email"]).strip()
        if extracted.get("recruiter_phone"):
            result["recruiter_phone"] = str(extracted["recruiter_phone"]).strip()

        logger.info(
            "linkedin_llm_fallback_succeeded",
            idx=idx, url=url,
            recovered_description=bool(result.get("description")),
            recovered_recruiter=bool(result.get("recruiter_name")),
            recovered_recruiter_contact=bool(result.get("recruiter_email") or result.get("recruiter_phone")),
        )
        return result
