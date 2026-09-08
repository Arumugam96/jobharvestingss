"""
LinkedIn Home Feed Lead Agent — turns Home Feed posts into IT hiring leads.

What this agent does
────────────────────
• Opens the authenticated recruiter's LinkedIn **Home Feed** (/feed/) — NOT the
  Jobs board and NOT LinkedIn's post/content search.
• Scrolls the dynamically-rendered feed, collecting each rendered post (author,
  headline, text, timestamp, repost flag, stable post URN → permalink).
• Runs a two-stage intelligent pipeline so most of the noisy feed never reaches
  the LLM:
    1. cheap deterministic candidate filter (hiring intent + IT terminology),
    2. LLM semantic classification (is this a genuine IT hiring post?),
    3. LLM extraction of job + recruiter info for the genuine ones only.
• Validates the extraction and assigns a lead_confidence from independent signals.

Security / integrity contract (identical to the other LinkedIn agents)
──────────────────────────────────────────────────────────────────────
• Reuses the authenticated session (session file / persistent Chrome profile).
• NO login automation. Auth is verified by the `li_at` cookie; when it is
  absent the run raises FeedAuthError and the caller tells the user to run
  POST /linkedin-setup-session.
• The LLM NEVER fabricates contact/job info — email/phone/company/title are kept
  only when explicitly present in the post text; otherwise they stay null.
• Only publicly-visible feed content is stored.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import structlog

from app.config import Settings, get_settings
from app.core.contact_normalize import normalize_email, normalize_phone
from app.core.dependencies import get_llm_service
from app.core.exceptions import LLMUnavailableError
from app.models.harvest_run import LlmCallType
from app.services import run_guard
from app.agents.linkedin_agent import (
    _clean,
    _infer_work_mode,
    _resolve_posted_date,
    _wait_for_page_text_stable,
)
from app.agents.linkedin_lead_agent import _HIRING_KEYWORDS
from app.agents.prospect_intelligence_agent import _HIRING_DOMAIN_MAP, _classify_hiring_domain

if TYPE_CHECKING:
    from playwright.async_api import Page

    from app.services.llm_service import LLMService

logger = structlog.get_logger(__name__)

FEED_URL = "https://www.linkedin.com/feed/"
_GATED_PATHS = ("/login", "/checkpoint", "/challenge", "/authwall", "/uas/")

# Batch size for the incremental-persistence callback — flush finalized leads to
# the DB every N so the frontend's live "leads saved" counter ticks steadily and
# a mid-run crash keeps what was already extracted.
_PERSIST_BATCH = 5

# Max chars of a single post's text handed to the LLM (a feed post is short; this
# is a safety cap for the occasional very long post).
_POST_TEXT_MAX_CHARS = 6_000

# Categories the classifier may assign. Only "it_hiring" is a keeper; the rest
# name the feed-noise buckets so the audit trail records *why* a post was dropped.
_FEED_CATEGORIES = (
    "it_hiring", "non_it_hiring", "hiring_trends", "hr_content",
    "ad_promo", "company_promo", "repost_no_info", "irrelevant", "other",
)

# IT / technology terminology for the candidate filter — flattened from the
# existing hiring-domain keyword map, plus a few generic role words. A post must
# contain hiring intent AND at least one of these to reach the LLM.
_IT_TERMS: set[str] = {
    kw.strip() for kws, _domain in _HIRING_DOMAIN_MAP for kw in kws if kw.strip()
} | {
    "developer", "engineer", "software", "programming", "programmer", "coding",
    "full stack", "fullstack", "backend", "back-end", "frontend", "front-end",
    "web developer", "data engineer", "data scientist", "devops", "sdet", "tester",
    "python", "java", "javascript", "typescript", "react", "angular", "node",
    ".net", "c#", "golang", "kubernetes", "aws", "azure", "gcp", "sql",
    "machine learning", "ai/ml", "ml engineer", "cloud", "cybersecurity",
    "it recruitment", "tech hiring", "software engineer", "sre", "qa engineer",
}
# Match IT terms on WORD BOUNDARIES so short domain tokens (e.g. the map's " ai "
# / " ml " / " bi ", which strip to "ai"/"ml"/"bi") can't substring-match inside
# ordinary words like "mumbai", "email", or "training" — that would let almost
# every post through and defeat the candidate filter's cost-saving purpose.
_IT_TERM_RE = re.compile(
    r"(?<![a-z0-9])(?:"
    + "|".join(sorted((re.escape(t) for t in _IT_TERMS if t), key=len, reverse=True))
    + r")(?![a-z0-9])",
    re.IGNORECASE,
)

StatusCallback = Callable[[str], Awaitable[None]]
BatchCallback = Callable[[list["FeedLead"]], Awaitable[None]]


# ══════════════════════════════════════════════════════════════════════════════
# Types
# ══════════════════════════════════════════════════════════════════════════════

class FeedAuthError(RuntimeError):
    """Raised when the Home Feed can't be reached as an authenticated user
    (no li_at cookie). The caller surfaces an action_required hint pointing at
    POST /linkedin-setup-session."""


@dataclass
class FeedLead:
    """One validated IT-hiring lead extracted from a Home Feed post."""
    # ── Job info ──────────────────────────────────────────────────────────────
    job_title:       str = ""
    company:         str = ""
    location:        str = ""
    salary:          str = ""
    employment_type: str = ""
    work_mode:       str = "not_specified"
    skills:          list[str] = field(default_factory=list)
    description:     str = ""
    posted_date:     str = ""              # YYYY-MM-DD (resolved in Python) or ""
    domain:          str = "IT"            # IT sub-domain (display only)
    # ── Recruiter info ────────────────────────────────────────────────────────
    recruiter_name:        str = ""
    recruiter_title:       str = ""
    recruiter_company:     str = ""
    recruiter_profile_url: str = ""
    recruiter_email:       str = ""        # only if verbatim in the post
    recruiter_phone:       str = ""        # only if verbatim in the post
    # ── Provenance / meta ─────────────────────────────────────────────────────
    post_url:        str = ""              # feed permalink (from URN)
    post_urn:        str = ""
    is_repost:       bool = False
    category:        str = "it_hiring"
    lead_confidence: float = 0.0

    def to_scraped_job_dict(self) -> dict[str, Any]:
        """Map onto ScrapedJobORM's canonical dict shape (see
        HarvestRunService.bulk_insert_scraped_jobs). source="LinkedIn Feed"
        distinguishes these rows from LinkedIn Jobs / Naukri / Dice."""
        return {
            "source":                 "LinkedIn Feed",
            "job_title":              self.job_title,
            "company":                self.company,
            "location":               self.location,
            "salary":                 self.salary,
            "experience":             "",
            "posted_date":            self.posted_date,
            "job_url":                self.post_url,
            "job_description":        self.description,
            "job_description_html":   "",
            "skills":                 self.skills,
            "work_mode":              self.work_mode,
            "company_url":            "",
            "employment_type":        self.employment_type,
            "job_type":               "",
            "domain":                 self.domain or "IT",
            "hiring_entity":          "Any",
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

def _is_candidate(text: str) -> bool:
    """Cheap first-stage gate: keep a post only if it shows hiring intent AND IT
    terminology. Broad recall — this only removes obviously-irrelevant posts to
    cut LLM cost; the LLM makes the actual keep/drop decision."""
    raw = text or ""
    if len(raw) < 12:
        return False
    t = f" {raw.lower()} "
    has_hiring = any(kw in t for kw in _HIRING_KEYWORDS)
    has_it = bool(_IT_TERM_RE.search(raw))
    return has_hiring and has_it


# ══════════════════════════════════════════════════════════════════════════════
# In-browser feed post collection
# ══════════════════════════════════════════════════════════════════════════════

# Enumerate feed post containers by STABLE attributes (data-urn / data-id carry
# the activity URN LinkedIn's own tracking depends on; role=listitem is driven by
# accessibility, not the hashed CSS-module build) — far more durable than any
# classname. For each post we return raw fields; Python does the dedup + parsing.
_COLLECT_POSTS_JS = r"""() => {
    function pickText(root, sels) {
        for (const s of sels) {
            const el = root.querySelector(s);
            if (el && el.textContent && el.textContent.trim()) return el.textContent.trim();
        }
        return '';
    }
    const containers = Array.from(document.querySelectorAll(
        'div.feed-shared-update-v2, ' +
        'div[data-urn*="urn:li:activity"], ' +
        'div[data-id*="urn:li:activity"], ' +
        'div[role="listitem"]'
    ));
    const out = [];
    const seen = new Set();
    for (const el of containers) {
        try {
            const urn = el.getAttribute('data-urn') || el.getAttribute('data-id') || '';
            // Skip nested/duplicate containers that share the same activity urn.
            const key = urn || (el.textContent || '').slice(0, 80);
            if (!key || seen.has(key)) continue;
            seen.add(key);

            // Author name — actor title, then any /in/ link text.
            let authorName = pickText(el, [
                '.update-components-actor__title span[aria-hidden="true"]',
                '.update-components-actor__name span[aria-hidden="true"]',
                '.update-components-actor__name',
                '.update-components-actor__title',
            ]);
            let profileUrl = '';
            const inLink = el.querySelector('a.update-components-actor__meta-link, a[href*="/in/"]');
            if (inLink) {
                profileUrl = inLink.href || inLink.getAttribute('href') || '';
                if (!authorName) {
                    authorName = (inLink.textContent || '').trim().split('\n')[0].trim();
                }
            }

            const headline = pickText(el, [
                '.update-components-actor__description',
                '.update-components-actor__subtitle',
            ]);

            // Timestamp — "2h", "1d • Edited", etc.
            const timestamp = pickText(el, [
                '.update-components-actor__sub-description span[aria-hidden="true"]',
                '.update-components-actor__sub-description',
                'time',
            ]);

            // Post body text (the author's commentary).
            let text = pickText(el, [
                '.update-components-text',
                '.feed-shared-update-v2__description',
                '.feed-shared-inline-show-more-text',
            ]);
            if (!text || text.length < 20) {
                text = (el.innerText || el.textContent || '').trim();
            }

            // Full container text — commentary PLUS any embedded LinkedIn job/entity
            // card (title, company, location, salary) and metadata. The job card is a
            // SEPARATE component from .update-components-text, so the body alone omits
            // those structured details; the whole container's innerText carries them,
            // and the LLM extractor is told to use only what is explicitly present.
            var fullText = (el.innerText || el.textContent || '').trim().slice(0, 5000);

            // Repost / reshare indicator.
            const header = (el.querySelector('.update-components-header')
                || el.querySelector('.update-components-actor__supplementary-actor-info'));
            const headerTxt = header ? (header.textContent || '').toLowerCase() : '';
            const isRepost = headerTxt.includes('repost') || headerTxt.includes('reshare');

            // Public email typed as a mailto link.
            const mailto = el.querySelector('a[href^="mailto:"]');
            const mailtoEmail = mailto ? (mailto.href || '').replace('mailto:', '').trim() : '';

            out.push({
                urn: urn,
                author_name: authorName,
                author_profile_url: profileUrl,
                author_headline: headline,
                timestamp: timestamp,
                text: text,
                full_text: fullText,
                is_repost: isRepost,
                mailto: mailtoEmail,
            });
        } catch (e) { /* skip malformed post */ }
    }
    return { count: containers.length, posts: out };
}"""


# ══════════════════════════════════════════════════════════════════════════════
# LinkedIn Feed Agent
# ══════════════════════════════════════════════════════════════════════════════

class LinkedInFeedAgent:
    """
    Harvests IT hiring leads from the authenticated LinkedIn Home Feed.

    One instance per run (mirrors LinkedInAgent). Owns an LLMService whose
    token usage + per-call audit log the caller persists to llm_calls.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        llm_service: "LLMService | None" = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._llm_service = llm_service or get_llm_service(self._settings)
        self._llm_calls_made = 0
        # Fixed once per run so every relative date ("2h ago") resolves against
        # the same reference point (see _resolve_posted_date).
        self._harvest_started_at = datetime.now(timezone.utc)

        s = self._settings
        self._max_posts        = int(s.linkedin_feed_max_posts)
        self._max_scrolls      = int(s.linkedin_feed_max_scrolls)
        self._min_scrolls      = int(s.linkedin_feed_min_scrolls)
        self._scroll_delay_ms  = int(s.linkedin_feed_scroll_delay_ms)
        self._no_new_rounds     = int(s.linkedin_feed_no_new_stop_rounds)
        self._empty_dom_rounds  = int(s.linkedin_feed_empty_dom_stop_rounds)
        self._llm_max_calls    = int(s.linkedin_feed_llm_max_calls)
        self._min_confidence   = float(s.linkedin_feed_min_confidence)

    def get_token_usage(self) -> dict:
        if self._llm_service is None:
            from app.services.llm_service import empty_usage_summary
            return empty_usage_summary()
        return self._llm_service.get_usage_summary()

    def get_llm_call_log(self) -> list[dict]:
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
    ) -> list[FeedLead]:
        """Open the Home Feed, collect posts, and return validated IT-hiring
        leads. Raises FeedAuthError when the session is not authenticated."""
        from app.scrapers.browser_manager import BrowserManager, PersistentBrowserManager
        from app.services.config_service import ConfigService
        from app.services.session_manager import SessionManager

        chrome_profile = ConfigService().load().browser.chrome_profile
        sm = SessionManager("linkedin")
        storage_state_arg = sm.storage_state_arg()

        if storage_state_arg:
            logger.debug("linkedin_feed_using_session_file", session_file=storage_state_arg)
            browser_ctx = BrowserManager(
                headless=headless, slow_mo=slow_mo, storage_state=storage_state_arg,
            )
        else:
            logger.warning(
                "linkedin_feed_no_session_file",
                hint="No data/sessions/linkedin_session.json — falling back to Chrome profile. "
                     "Run POST /linkedin-setup-session to create it.",
            )
            browser_ctx = PersistentBrowserManager(
                profile_dir=chrome_profile, headless=headless, slow_mo=slow_mo,
            )

        async with browser_ctx as bm:
            page = await bm.new_page()
            await self._open_feed(page)
            raw_posts = await self._collect_posts(page, on_status=on_status)
            leads = await self._process_posts(raw_posts, on_status=on_status, on_batch=on_batch)

        logger.info(
            "linkedin_feed_harvest_completed",
            posts_collected=len(raw_posts),
            leads=len(leads),
            llm_calls=self._llm_calls_made,
        )
        return leads

    # ── Open + auth-gate the feed ───────────────────────────────────────────────

    async def _open_feed(self, page: "Page") -> None:
        try:
            await page.goto(FEED_URL, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(3_000)
        except Exception as exc:
            logger.error("linkedin_feed_navigation_failed", error=str(exc))
            raise FeedAuthError(f"Could not open LinkedIn Home Feed: {exc}") from exc

        await self._dismiss_overlays(page)

        # Auth is confirmed by the li_at cookie, not URL shape (LinkedIn will show
        # a guest shell without redirecting) — same check as the other agents.
        try:
            cookies = await page.context.cookies()
        except Exception:
            cookies = []
        if not any(c.get("name") == "li_at" for c in cookies) or any(p in page.url for p in _GATED_PATHS):
            logger.warning("linkedin_feed_not_authenticated", url=page.url[:80])
            raise FeedAuthError(
                "LinkedIn Home Feed is not authenticated (no li_at cookie). "
                "Run POST /linkedin-setup-session to log in once."
            )
        logger.info("linkedin_feed_ready", url=page.url[:80])

    async def _dismiss_overlays(self, page: "Page") -> None:
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

    # ── Scroll-collect the rendered feed ────────────────────────────────────────

    async def _collect_posts(
        self, page: "Page", on_status: StatusCallback | None = None,
    ) -> list[dict]:
        """Scroll the feed, accumulating de-duplicated posts (keyed by URN), until
        any env-configured stop condition trips: max posts, max scrolls, N scrolls
        with no new posts, or M scrolls returning an empty DOM. Cooperative-stop
        aware."""
        collected: dict[str, dict] = {}   # urn/key → raw post
        no_new_streak = 0
        empty_dom_streak = 0
        prev_height = 0

        await _wait_for_page_text_stable(page, stable_rounds=3, max_polls=30)

        for scroll_i in range(self._max_scrolls + 1):
            if run_guard.is_stop_requested():
                logger.info("linkedin_feed_collect_stopped_by_user", scroll=scroll_i, collected=len(collected))
                break

            try:
                result = await page.evaluate(_COLLECT_POSTS_JS)
            except Exception as exc:
                logger.warning("linkedin_feed_collect_eval_failed", scroll=scroll_i, error=str(exc))
                result = {"count": 0, "posts": []}

            dom_count = int(result.get("count", 0))
            posts = result.get("posts", []) or []

            # Empty-DOM signal — feed exhausted or a DOM change broke matching.
            if dom_count == 0:
                empty_dom_streak += 1
                logger.info("linkedin_feed_empty_dom", scroll=scroll_i, streak=empty_dom_streak)
                if empty_dom_streak >= self._empty_dom_rounds:
                    logger.info("linkedin_feed_stop_empty_dom", scroll=scroll_i)
                    break
            else:
                empty_dom_streak = 0

            added = 0
            for p in posts:
                key = (p.get("urn") or "").strip() or _clean(p.get("text", ""))[:80]
                if not key or key in collected:
                    continue
                collected[key] = p
                added += 1
                if len(collected) >= self._max_posts:
                    break

            # No-new-posts signal — distinct from empty DOM (posts render but all
            # are duplicates: natural end of fresh feed content).
            if added == 0:
                no_new_streak += 1
            else:
                no_new_streak = 0

            if on_status:
                await on_status(f"Collecting feed posts… {len(collected)} gathered")
            logger.info(
                "linkedin_feed_scroll",
                scroll=scroll_i, dom_count=dom_count, new=added,
                total=len(collected), no_new_streak=no_new_streak,
            )

            if len(collected) >= self._max_posts:
                logger.info("linkedin_feed_stop_max_posts", total=len(collected))
                break
            # Honour the "no new posts" early-stop only after a minimum number of
            # scrolls — IT hiring posts sit deep in the feed and LinkedIn often
            # serves a barren batch before loading more, so stopping at the first
            # few quiet scrolls would miss them.
            if no_new_streak >= self._no_new_rounds and scroll_i >= self._min_scrolls:
                logger.info("linkedin_feed_stop_no_new", scroll=scroll_i)
                break
            if scroll_i >= self._max_scrolls:
                logger.info("linkedin_feed_stop_max_scrolls", scroll=scroll_i)
                break

            # Load more posts, then measure whether the document actually grew. If
            # it did, the infinite-scroll loader fired — reset the no-new streak so
            # a slow-rendering batch (posts appended but not yet parsed) isn't
            # mistaken for the end of the feed.
            new_height = await self._scroll_feed(page)
            if new_height > prev_height + 50:
                no_new_streak = 0
            prev_height = new_height

        logger.info("linkedin_feed_collect_done", total=len(collected))
        return list(collected.values())

    async def _scroll_feed(self, page: "Page") -> int:
        """Trigger the Home Feed's infinite-scroll loader and return the document
        scroll height afterwards (so the caller can detect real growth).

        The feed lazy-loads via an IntersectionObserver near the bottom. The old
        code only did window.scrollTo(0, body.scrollHeight) + a wheel event at the
        default pointer position (0,0) — which lands on the fixed top nav and
        scrolls nothing — so the loader never fired and the DOM stayed pinned at
        the first ~11 posts. Three complementary nudges make it fire reliably:
          1. scroll the document to the bottom (the feed scrolls on the window);
          2. pull the LAST rendered post into view — scrollIntoView walks up to
             whatever ancestor is actually scrollable, so it works whether the
             feed scrolls on the window or an inner overflow container;
          3. a real wheel gesture over the CENTRE of the 1366×900 viewport.
        """
        try:
            await page.evaluate(
                """() => {
                    const scroller = document.scrollingElement || document.documentElement;
                    scroller.scrollTo(0, scroller.scrollHeight);
                    const posts = document.querySelectorAll('div.feed-shared-update-v2, div[data-urn*="urn:li:activity"], div[data-id*="urn:li:activity"], div[role="listitem"]');
                    if (posts.length) posts[posts.length - 1].scrollIntoView({block: 'end'});
                    window.dispatchEvent(new Event('scroll'));
                }"""
            )
        except Exception as exc:
            logger.debug("linkedin_feed_scroll_eval_failed", error=str(exc))
        try:
            await page.mouse.move(683, 450)   # centre of the viewport, not (0,0)
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

    # ── Two-stage LLM processing ────────────────────────────────────────────────

    async def _process_posts(
        self,
        raw_posts: list[dict],
        on_status: StatusCallback | None = None,
        on_batch: BatchCallback | None = None,
    ) -> list[FeedLead]:
        leads: list[FeedLead] = []
        pending: list[FeedLead] = []
        diag: list[dict] = []            # per-post trace → feed_diag.json
        cat_counts: dict[str, int] = {}  # classifier category histogram
        candidates = 0
        classified_yes = 0

        for idx, raw in enumerate(raw_posts):
            if run_guard.is_stop_requested():
                logger.info("linkedin_feed_process_stopped_by_user", processed=idx)
                break

            # Prefer the full container text (commentary + embedded job card) so the
            # card's company/location/salary reach the candidate filter and the LLM;
            # fall back to the post body when the container text wasn't captured.
            text = _clean(raw.get("full_text") or raw.get("text", ""))
            rec: dict = {
                "idx":       idx,
                "author":    raw.get("author_name", ""),
                "is_repost": bool(raw.get("is_repost")),
                "text_len":  len(text),
                "snippet":   text[:280],
                "stage":     "collected",
            }

            # Stage 1 — deterministic candidate filter (no LLM).
            if not _is_candidate(text):
                rec["stage"] = "filtered_out_not_candidate"
                diag.append(rec)
                continue
            candidates += 1
            rec["stage"] = "candidate"

            if self._llm_calls_made >= self._llm_max_calls:
                logger.warning("linkedin_feed_llm_cap_reached", cap=self._llm_max_calls, idx=idx)
                rec["stage"] = "skipped_llm_cap"
                diag.append(rec)
                break

            try:
                # Stage 2 — LLM classify.
                classification = await self._classify(text, raw)
                cat = str(classification.get("category") or "unknown")
                rec.update(
                    category=cat,
                    is_it_hiring=classification.get("is_it_hiring"),
                    it_relevance=classification.get("it_relevance"),
                    confidence=classification.get("confidence"),
                )
                cat_counts[cat] = cat_counts.get(cat, 0) + 1
                if not self._is_keeper(classification):
                    rec["stage"] = "classified_not_it_hiring"
                    diag.append(rec)
                    continue
                classified_yes += 1

                if self._llm_calls_made >= self._llm_max_calls:
                    logger.warning("linkedin_feed_llm_cap_reached", cap=self._llm_max_calls, idx=idx)
                    rec["stage"] = "skipped_llm_cap_before_extract"
                    diag.append(rec)
                    break

                # Stage 3 — LLM extract (positives only).
                extracted = await self._extract(text, raw)
                lead = self._finalize(raw, text, classification, extracted)
                if lead is None:
                    rec["stage"] = "dropped_no_job_title"
                    diag.append(rec)
                    continue
                rec["stage"] = "lead"
                rec["lead_confidence"] = lead.lead_confidence
            except LLMUnavailableError as exc:
                # Provider down — stop processing further posts but keep whatever
                # was already extracted (the caller persists the partial set).
                logger.error("linkedin_feed_llm_unavailable", error=str(exc), processed=idx)
                rec["stage"] = "llm_unavailable"
                diag.append(rec)
                break
            except Exception as exc:
                # One bad post must never abort the whole harvest.
                logger.warning("linkedin_feed_post_failed", idx=idx, error=str(exc))
                rec["stage"] = "error"
                rec["error"] = str(exc)
                diag.append(rec)
                continue

            diag.append(rec)
            leads.append(lead)
            pending.append(lead)
            if on_batch and len(pending) >= _PERSIST_BATCH:
                await self._flush(pending, on_batch, on_status, len(leads))
                pending = []

        if pending and on_batch:
            await self._flush(pending, on_batch, on_status, len(leads))

        logger.info(
            "linkedin_feed_processing_done",
            posts=len(raw_posts), candidates=candidates,
            classified_it_hiring=classified_yes, leads=len(leads),
            categories=cat_counts,
        )
        self._write_diag(diag, cat_counts, len(raw_posts), candidates, classified_yes, len(leads))
        return leads

    @staticmethod
    def _write_diag(
        diag: list[dict], cat_counts: dict[str, int],
        posts: int, candidates: int, classified_yes: int, leads: int,
    ) -> None:
        """Best-effort per-run diagnostic dump so a '0 leads' run is explainable:
        every collected post with its text snippet and the stage it stopped at
        (filtered_out_not_candidate / classified_not_it_hiring / lead / …), plus a
        summary + category histogram. Written to the bind-mounted data dir
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
                            "posts_collected":       posts,
                            "candidates":            candidates,
                            "classified_it_hiring":  classified_yes,
                            "leads":                 leads,
                            "category_counts":       cat_counts,
                        },
                        "posts": diag,
                    },
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
            logger.info("linkedin_feed_diag_written", path=str(d / "feed_diag.json"), summary_leads=leads)
        except Exception as exc:
            logger.debug("linkedin_feed_diag_write_failed", error=str(exc))

    async def _flush(
        self,
        pending: list[FeedLead],
        on_batch: BatchCallback,
        on_status: StatusCallback | None,
        total: int,
    ) -> None:
        try:
            await on_batch(list(pending))
            if on_status:
                await on_status(f"Extracting IT hiring leads… {total} saved")
        except Exception as exc:
            logger.warning("linkedin_feed_batch_flush_failed", error=str(exc))

    def _is_keeper(self, c: dict) -> bool:
        """Accept only a genuine IT hiring post above the confidence gate."""
        try:
            conf = float(c.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        return bool(
            c.get("is_it_hiring")
            and c.get("it_relevance")
            and c.get("category") == "it_hiring"
            and conf >= self._min_confidence
        )

    # ── Stage 2: classification ─────────────────────────────────────────────────

    async def _classify(self, text: str, raw: dict) -> dict:
        headline = _clean(raw.get("author_headline", ""))
        content = (
            f"---AUTHOR HEADLINE---\n{headline or '(none)'}\n\n"
            f"---POST TEXT---\n{text[:_POST_TEXT_MAX_CHARS]}"
        )
        schema = (
            '{"is_it_hiring": bool (true ONLY if this post is a real, current job '
            'opening / hiring call for an IT/software/technology role at a specific '
            "company — judge the whole post's meaning, not the mere presence of "
            'hiring words), '
            '"it_relevance": bool (true if the role(s) are IT/software/technology — '
            "software/backend/frontend/full-stack engineers, data engineers/"
            "scientists, ML/AI, DevOps/cloud/SRE, QA/automation, cybersecurity, "
            "database, mobile, etc.), "
            f'"category": one of {list(_FEED_CATEGORIES)} (it_hiring = a genuine IT '
            "hiring post; hiring_trends = commentary/discussion about hiring, NOT an "
            "opening; hr_content = general HR/recruitment content; ad_promo = "
            "advertisement/sponsored; company_promo = company promotion; "
            "repost_no_info = a repost with no real hiring detail; non_it_hiring = "
            "hiring but not IT; irrelevant/other otherwise), "
            '"confidence": number between 0 and 1 (your confidence in is_it_hiring), '
            '"reason": str (one short sentence)}'
        )
        system = (
            "You classify a single LinkedIn Home Feed post. Decide from the "
            "SEMANTIC MEANING of the entire post, never from keyword presence. A "
            "post that a company is 'growing its engineering team and looking for "
            "Python developers' IS a genuine IT hiring post (is_it_hiring=true, "
            "category=it_hiring). A post 'discussing current hiring trends in the "
            "IT industry' is NOT (category=hiring_trends, is_it_hiring=false). Ads, "
            "company promotion, generic HR content, and reposts with no concrete "
            "opening are not hiring posts. Return only the schema fields, no "
            "commentary."
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
        headline = _clean(raw.get("author_headline", ""))
        author_name = _clean(raw.get("author_name", ""))
        profile_url = (raw.get("author_profile_url") or "").strip()
        mailto = (raw.get("mailto") or "").strip()

        # Pre-resolve the posting date in Python (never ask the LLM to do date math).
        resolved_date = _resolve_posted_date(
            _clean(raw.get("timestamp", "")), self._harvest_started_at
        ) or _resolve_posted_date(text, self._harvest_started_at)

        schema = (
            '{"job_title": str (the IT role being hired for; empty if not stated), '
            '"company": str (the hiring company; empty if not stated — do NOT guess), '
            '"location": str (city/region and Remote/Hybrid/On-site if stated; empty otherwise), '
            '"salary": str (empty if not disclosed), '
            '"employment_type": str (e.g. Full-time, Contract; empty if not stated), '
            '"skills": list[str] (technologies/skills explicitly named; empty list if none), '
            '"description": str (the post\'s hiring details verbatim; empty if none), '
            '"posted": str or null (copy the pre-calculated date given below verbatim; '
            "do not derive it yourself; null if marked unavailable), "
            '"recruiter_name": str or null (the poster/recruiter name, null if unknown), '
            '"recruiter_title": str or null (their designation, null if unknown), '
            '"recruiter_company": str or null (only if explicitly stated), '
            '"recruiter_email": str or null (ONLY if an email appears verbatim in the '
            "text — never guess or construct one; if masked/partial, null), "
            '"recruiter_phone": str or null (ONLY if a phone number appears verbatim — '
            "never guess; E.164 with country code when unambiguous; if masked/partial, null)}"
        )
        system = (
            "You extract structured data from a single LinkedIn hiring post's text. "
            "Extract ONLY what is explicitly present. NEVER fabricate, infer, or "
            "construct a company, email, phone, location, salary, or requirement "
            "that is not literally in the text (e.g. do not build an email from a "
            "name and company). If a field is not present, return empty/null. "
            "Return only the schema fields, no commentary."
        )
        content = (
            f"---POSTER---\n{author_name or '(unknown)'} — {headline or '(no headline)'}\n"
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
        return result

    # ── Validate + confidence ───────────────────────────────────────────────────

    def _finalize(
        self, raw: dict, text: str, classification: dict, extracted: dict,
    ) -> FeedLead | None:
        """Build a validated FeedLead. Returns None if the extraction has no
        usable job title (can't be a job lead without one)."""
        job_title = str(extracted.get("job_title") or "").strip()
        if not job_title:
            logger.info("linkedin_feed_lead_dropped_no_title", urn=raw.get("urn", "")[:40])
            return None

        # Recruiter identity — prefer the DOM-scraped author (reliable) over the
        # LLM's echo; contact only if verbatim (deterministic masking rejection).
        author_name = _clean(raw.get("author_name", "")) or str(extracted.get("recruiter_name") or "").strip()
        profile_url = (raw.get("author_profile_url") or "").split("?")[0].strip()
        if profile_url and "/in/" not in profile_url.lower():
            profile_url = ""  # company/school link, not a recruiter profile

        email = normalize_email(extracted.get("recruiter_email")) or ""
        phone = normalize_phone(extracted.get("recruiter_phone")) or ""
        # A mailto: link scraped straight from the DOM is authoritative too.
        if not email and raw.get("mailto"):
            email = normalize_email(raw.get("mailto")) or ""

        company = str(extracted.get("company") or "").strip()
        location = str(extracted.get("location") or "").strip()
        skills = [str(s).strip() for s in (extracted.get("skills") or []) if str(s).strip()][:20]
        description = str(extracted.get("description") or "").strip()[:20_000]
        posted = extracted.get("_resolved_date") or (
            str(extracted.get("posted")).strip() if extracted.get("posted") else ""
        )

        # IT sub-domain tag (display only) — reuse the shared classifier.
        domain = _classify_hiring_domain("", _clean(raw.get("author_headline", "")), [job_title, description])
        domain = (domain.split(",")[0].strip() if domain and domain != "NOT_FOUND" else "IT")

        lead = FeedLead(
            job_title=job_title,
            company=company,
            location=location,
            salary=str(extracted.get("salary") or "").strip(),
            employment_type=str(extracted.get("employment_type") or "").strip(),
            work_mode=_infer_work_mode(f"{location} {description}"),
            skills=skills,
            description=description,
            posted_date=posted,
            domain=domain,
            recruiter_name=author_name,
            recruiter_title=str(extracted.get("recruiter_title") or "").strip()
                or _clean(raw.get("author_headline", "")),
            recruiter_company=str(extracted.get("recruiter_company") or "").strip(),
            recruiter_profile_url=profile_url,
            recruiter_email=email,
            recruiter_phone=phone,
            post_url=self._permalink(raw.get("urn", "")),
            post_urn=str(raw.get("urn") or ""),
            is_repost=bool(raw.get("is_repost")),
            category="it_hiring",
        )
        lead.lead_confidence = self._score(lead, classification)
        logger.info(
            "linkedin_feed_lead",
            title=lead.job_title, company=lead.company,
            recruiter=lead.recruiter_name, has_email=bool(lead.recruiter_email),
            has_phone=bool(lead.recruiter_phone), confidence=lead.lead_confidence,
        )
        return lead

    def _score(self, lead: FeedLead, classification: dict) -> float:
        """Blend the classifier's own confidence with a count of independent
        supporting signals — a lead is stronger when several agree. No artificial
        filler: each signal is a real, present piece of evidence."""
        try:
            llm_conf = float(classification.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            llm_conf = 0.0
        llm_conf = max(0.0, min(1.0, llm_conf))

        signals = [
            _is_candidate(lead.description or ""),           # explicit hiring + IT language
            bool(lead.job_title),                            # clear IT job title
            bool(classification.get("it_relevance")),        # IT relevance
            bool(lead.recruiter_name),                       # recruiter identity
            bool(lead.company),                              # company identity
            bool(lead.recruiter_email or lead.recruiter_phone),  # public contact
        ]
        signal_fraction = sum(1 for s in signals if s) / len(signals)
        return round(0.5 * llm_conf + 0.5 * signal_fraction, 3)

    @staticmethod
    def _permalink(urn: str) -> str:
        urn = (urn or "").strip()
        if urn.startswith("urn:li:activity"):
            return f"https://www.linkedin.com/feed/update/{urn}/"
        return ""
