"""
Resdex candidate search — drives the Naukri Resdex advanced search in the
logged-in RECRUITER profile (naukri_recruiter_agent) and returns the result
cards for app/services/resdex_match_service.py.

How a search runs (verified live against resdex.naukri.com, 2026-10-14)
────────────────────────────────────────────────────────────────────────
  • Page: https://resdex.naukri.com/v3?activeTab=advSrch — a slow SPA; wait for
    the keyword box (input[name=ezKeywordsAny]).
  • Boolean mode: the role=switch checkbox #booleanKeywordsSearch (click its
    label). With it on, the keyword box is swapped for input[name=boolKeywords]
    and its text is sent as `boolKeywords` with `booleanSearch: true`; off, the
    ezKeywordsAny box turns the text into one keyword chip.
  • Filters: input[name=minExp] / input[name=maxExp]; input[name=locations] is a
    suggester — type the city, pick the matching `.opt.tuple-wrap` suggestion.
  • Submit #adv-search-btn → POST …/recruiter-js-profile-listing-services/v0/
    search/results/advSearch, whose JSON (`totalResumes`, `tuples` — 40 per page)
    is intercepted: structured name / designation / employer / experience /
    skills / education / notice / CTC / activity, so no LLM is needed for cards.
  • Each result renders as .tuple[data-tuple-id=<jsUserId>] with the profile
    link in .candidate-headline a[href*="/v3/preview"] — joined to the JSON by
    jsUserId. That link is bound to this search in this browser session
    (sid, storageKey, per-search dynamicEncrypted* keys) and Naukri rejects it
    anywhere else, so it is NOT stored as profile_url. Instead each card keeps
    the search id (`sid`, JSON top level) and its 0-based `tupleIndex` in raw;
    the job page links the reopenable result list (resdex_types.resdex_results_url).

Never opens a profile, reveals a contact or downloads a CV (those spend Resdex
credits). Never automates login — a dead session reports session_expired.

Anti-automation pacing
──────────────────────
  • Every click (Boolean switch label, field focus, location suggestion, Search)
    is a gradual pointer move + click (human_click), plain click as fallback.
  • After results load the list is scrolled and read for a random
    RESDEX_RESULTS_DWELL_MIN_S..MAX_S — no candidate is ever clicked.
  • Between searches: the RESDEX_DELAY_* gap, plus every
    RESDEX_BREAK_EVERY_SEARCHES searches a RESDEX_BREAK_MIN_S..MAX_S break
    (interruptible by a harvest stop).
  • A CAPTCHA / bot wall (after opening the form or submitting) is held in the
    Live Browser for a human for up to RESDEX_HUMAN_WAIT_S, then the search is
    retried; unsolved → the batch stops. CAPTCHAs are never solved automatically.
    A redirect to a recruiter login page ends the batch as session_expired.
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import structlog

from app.models.resdex import ResdexSearchStatus
from app.services.resdex_types import (
    ResdexBatchResult,
    ResdexCard,
    ResdexJobQuery,
    ResdexTier,
    preview_link_context,
    resdex_sid,
)

logger = structlog.get_logger(__name__)

RESDEX_SEARCH_URL = "https://resdex.naukri.com/v3?activeTab=advSrch"
_SEARCH_API = "search/results/advSearch"
_KEYWORD_BOX = 'input[name="ezKeywordsAny"], textarea[name="ezKeywordsAny"]'   # chip mode
_BOOLEAN_BOX = 'input[name="boolKeywords"], textarea[name="boolKeywords"]'   # replaces it in Boolean mode
_BOOLEAN_SWITCH = "#booleanKeywordsSearch"
_SEARCH_BTN = "#adv-search-btn"
_SUGGESTION = ".opt.tuple-wrap"
_MAX_LOCATIONS = 3
_MONTH_MS = 30 * 24 * 3600 * 1000

_LINKS_JS = """() => {
  const out = {};
  document.querySelectorAll('.tuple[data-tuple-id]').forEach(t => {
    const a = t.querySelector('.candidate-headline a[href*="/v3/preview"]')
           || t.querySelector('a[href*="/v3/preview"]');
    if (a) out[t.getAttribute('data-tuple-id')] = a.getAttribute('href');
  });
  return out;
}"""

_TEXT_CARDS_JS = """() => Array.from(document.querySelectorAll('.tuple[data-tuple-id]')).map(t => {
  const a = t.querySelector('a[href*="/v3/preview"]');
  const v = t.querySelector('.candidate-verification-status');
  return {id: t.getAttribute('data-tuple-id'), text: (t.innerText || '').slice(0, 4000),
          href: a ? a.getAttribute('href') : '',
          status: v ? (v.innerText || '').trim().slice(0, 200) : ''};
})"""

# ── CAPTCHA / bot-wall / session detection ───────────────────────────────────
# Same markers as the Naukri harvest's NaukriAgent._block_reason/_is_hard_captcha
# (naukri_agent.py), plus hCaptcha and the Akamai "Access Denied" body. reCAPTCHA
# frames are matched on the CHALLENGE / visible-checkbox frames only — an
# invisible reCAPTCHA (v3 / Enterprise badge) on a normal page is not a wall.
_CHALLENGE_SELECTORS = (
    "#challenge-form",
    "iframe[src*='challenges.cloudflare.com']",
    "iframe[src*='/recaptcha/'][src*='bframe']",
    "iframe[src*='/recaptcha/api2/anchor']:not([src*='size=invisible'])",
    "iframe[src*='/recaptcha/enterprise/anchor']:not([src*='size=invisible'])",
    "iframe[src*='hcaptcha.com'][src*='frame=challenge']",
    "iframe[src*='hcaptcha.com'][src*='frame=checkbox']",
)
# Page-text markers — only checked when no result tuples are rendered (a résumé
# on the results page must never trip them).
_CHALLENGE_TEXT = (
    "i'm not a robot",
    "check the box to let us know you",
    "unusual traffic",
    "verify you are human",
    "are you a robot",
    "you don't have permission to access",
)
_CHALLENGE_JS = """(sels) => {
  const vis = (el) => {
    const st = window.getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && el.getClientRects().length > 0;
  };
  const hit = sels.find((s) => {
    try { return Array.from(document.querySelectorAll(s)).some(vis); } catch (e) { return false; }
  });
  const tuples = document.querySelectorAll('.tuple[data-tuple-id]').length;
  const text = tuples ? '' : ((document.body && document.body.innerText) || '').slice(0, 6000);
  return {hit: hit || '', title: document.title || '', text: text.toLowerCase()};
}"""
_HUMAN_POLL_S = 5.0          # a held challenge is re-checked this often
_MAX_CHALLENGE_RETRIES = 3   # human-solved challenges per search before giving up


class ResdexChallenge(Exception):
    """A CAPTCHA / bot wall is showing instead of the Resdex page."""


class ResdexSessionExpired(Exception):
    """Resdex redirected to a recruiter login page — the session is gone."""


async def detect_challenge(page: Any) -> str | None:
    """Why `page` is a CAPTCHA / bot wall rather than Resdex, or None. No side
    effects; an evaluation error (mid-navigation) counts as "no challenge"."""
    url = str(getattr(page, "url", "") or "")
    try:
        path = (urlparse(url).path or "").lower()
    except ValueError:
        path = ""
    if "/challenge" in path or "captcha" in path:
        return f"challenge page {url.split('?')[0]}"
    try:
        info = await page.evaluate(_CHALLENGE_JS, list(_CHALLENGE_SELECTORS))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(info, dict):
        return None
    if "access denied" in str(info.get("title") or "").lower():
        return "Access Denied (bot wall)"
    if info.get("hit"):
        return f"captcha ({info['hit']})"
    text = str(info.get("text") or "")
    for marker in _CHALLENGE_TEXT:
        if marker in text:
            return f"bot check ({marker!r} on page)"
    return None


def _on_login_page(page: Any) -> bool:
    from app.agents.naukri_recruiter_agent import is_login_url

    return is_login_url(str(getattr(page, "url", "") or ""))


# ── Pure parsing (unit-tested against a probe fixture) ───────────────────────

def _split_csv(value: Any) -> list[str]:
    seen: list[str] = []
    for part in str(value or "").split(","):
        p = part.strip()
        if p and p.lower() not in {s.lower() for s in seen}:
            seen.append(p)
    return seen


def _role(entry: Any) -> dict[str, str] | None:
    if not isinstance(entry, dict):
        return None
    role = {
        "designation": str(entry.get("designation") or "").strip(),
        "organization": str(entry.get("organization") or "").strip(),
        "start": str(entry.get("startDate") or "").strip(),
        "end": str(entry.get("endDate") or "").strip(),
    }
    return role if role["designation"] or role["organization"] else None


def _education(edu: Any) -> str:
    """Highest qualification as shown: "Course, Specialization — Institute (Year)"."""
    if not isinstance(edu, dict):
        return ""
    for level in ("ppg", "pg", "ug"):
        e = edu.get(level)
        if not isinstance(e, dict):
            continue
        course = ", ".join(x for x in (str(e.get("course") or "").strip(),
                                       str(e.get("specialization") or "").strip()) if x)
        inst = str(e.get("institute") or "").strip()
        year = e.get("year")
        text = course
        if inst:
            text = f"{text} — {inst}" if text else inst
        if year:
            text = f"{text} ({year})"
        if text:
            return text
    return ""


def _ctc(info: Any) -> str:
    if not isinstance(info, dict):
        return ""
    lacs, thousands = str(info.get("lacs") or "").strip(), str(info.get("thousands") or "").strip()
    if not lacs and not thousands:
        return ""
    cur = str(info.get("currency") or "INR").strip()
    parts = [f"{lacs} Lacs" if lacs and lacs != "0" else "",
             f"{thousands} Thousand" if thousands and thousands != "0" else ""]
    body = " ".join(p for p in parts if p) or "0 Lacs"
    return f"{cur} {body}"


def parse_resdex_json(
    payload: dict[str, Any], links: dict[str, str] | None = None,
    *, active_within_months: int = 0, now_ms: int | None = None,
) -> list[ResdexCard]:
    """advSearch JSON → normalized cards (CANDIDATE_FIELDS), in Resdex order.

    `links` maps jsUserId → the rendered card's /v3/preview href. That href is
    session-bound (see module doc), so it is never stored as profile_url — it
    only backs up the search id when the payload lacks `sid`. Each card's raw
    keeps `sid` and its 0-based `tupleIndex` in the result list. Candidates
    whose `activeDateMillis` is older than `active_within_months` are dropped
    (0 = keep all). Only values present in the payload are stored."""
    links = links or {}
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    payload_sid = resdex_sid(payload.get("sid"))
    cards: list[ResdexCard] = []
    for index, t in enumerate(payload.get("tuples") or []):
        if not isinstance(t, dict):
            continue
        active_ms = int(t.get("activeDateMillis") or 0)
        if active_within_months and active_ms and now_ms - active_ms > active_within_months * _MONTH_MS:
            continue
        emp = t.get("employment") or {}
        current = _role(emp.get("current")) or {}
        previous = [r for r in [_role(emp.get("previous"))] + [_role(h) for h in (t.get("historicalEmployment") or [])] if r]
        exp = t.get("experience") or {}
        years, months = exp.get("years"), exp.get("months")
        exp_years = None
        exp_text = ""
        if isinstance(years, (int, float)):
            m = months if isinstance(months, (int, float)) else 0
            exp_years = round(years + m / 12, 1)
            exp_text = f"{int(years)}y {int(m)}m"
        skills = _split_csv(t.get("keySkills"))
        for s in _split_csv(t.get("focusedSkills")):
            if s.lower() not in {x.lower() for x in skills}:
                skills.append(s)
        uid = str(t.get("jsUserId") or "")
        href_sid, href_index = preview_link_context(links.get(uid, ""))
        sid = payload_sid or href_sid
        fields = {
            "candidate_name": str(t.get("jsUserName") or "").strip(),
            "headline": str(t.get("jobTitle") or "").strip(),
            "current_designation": current.get("designation", ""),
            "current_company": current.get("organization", ""),
            "previous_roles": previous,
            "experience_text": exp_text,
            "experience_years": exp_years,
            "location": str(t.get("currentLocation") or "").strip(),
            "preferred_locations": _split_csv(t.get("preferredLocations")),
            "skills": skills,
            "education": _education(t.get("education")),
            "notice_period": str(t.get("noticePeriodLabel") or "").strip(),
            "ctc": _ctc(t.get("ctcInfo")),
            "last_active": str(t.get("activeDateLabel") or "").strip(),
            # No stable per-candidate Resdex URL exists in the payload; the card's
            # /v3/preview href is session-bound and would open as "not valid".
            "profile_url": "",
        }
        raw = {
            "jsUserId": t.get("jsUserId"), "jsResId": t.get("jsResId"),
            # Search id + place in its result list → the job page links the
            # reopenable result list (resdex_types.resdex_link_context).
            "sid": sid or None,
            "tupleIndex": href_index if href_index is not None else index,
            "activeDateMillis": t.get("activeDateMillis"), "modifyDateMillis": t.get("modifyDateMillis"),
            "fresher": t.get("fresher"), "cvAttached": t.get("cvAttached"),
            "noticePeriodCode": t.get("noticePeriod"), "expectedCtcLacs": t.get("expectedCtcLacs"),
            # Naukri's own verification flags (resdex_types.naukri_verified).
            "emailVerified": t.get("emailVerified"), "phoneStatus": t.get("phoneStatus"),
            "conciergeValidatedLabel": t.get("conciergeValidatedLabel"),
            "conciergeValidatedOn": t.get("conciergeValidatedOn"),
        }
        cards.append(ResdexCard(method="json", fields=fields, profile_url=fields["profile_url"], raw=raw))
    return cards


# ── Browser mechanics ────────────────────────────────────────────────────────

class ResdexCandidateAgent:
    """One Resdex advanced search on an already-open recruiter-profile page."""

    def __init__(self, page: Any) -> None:
        self._page = page

    async def _click(self, element: Any) -> None:
        """Gradual pointer move + click (human_click); plain click fallback."""
        from app.scrapers.human import human_click_or_click

        await human_click_or_click(self._page, element)

    async def _guard(self) -> None:
        """Raise ResdexSessionExpired on a recruiter login page, ResdexChallenge
        on a CAPTCHA / bot wall; return quietly on a normal page."""
        if _on_login_page(self._page):
            raise ResdexSessionExpired(str(self._page.url).split("?")[0])
        reason = await detect_challenge(self._page)
        if reason:
            raise ResdexChallenge(reason)

    async def _open_form(self) -> None:
        page = self._page
        await page.goto(RESDEX_SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
        await self._guard()
        try:
            await page.locator(_KEYWORD_BOX).first.wait_for(state="visible", timeout=60_000)
        except Exception:
            await self._guard()   # a wall / login that rendered after domcontentloaded
            raise
        await page.wait_for_timeout(random.randint(1_200, 2_500))

    async def _boolean_on(self) -> bool:
        page = self._page
        switch = page.locator(_BOOLEAN_SWITCH)
        if not await switch.count():
            return False
        if not await switch.first.is_checked():
            await self._click(page.locator(f'label[for="{_BOOLEAN_SWITCH[1:]}"]').first)
            await page.wait_for_timeout(random.randint(600, 1_200))
        return await switch.first.is_checked()

    async def _fill(self, selector: str, value: str) -> bool:
        from app.scrapers.human import human_type

        box = self._page.locator(selector)
        if not await box.count():
            return False
        await self._click(box.first)
        await human_type(self._page, value)
        await self._page.wait_for_timeout(random.randint(400, 900))
        return True

    async def _add_location(self, city: str) -> bool:
        """Type into the location suggester and pick the suggestion that starts
        with `city`; anything else is abandoned (Escape) — never a wrong city."""
        from app.scrapers.human import human_type

        page = self._page
        box = page.locator('input[name="locations"]')
        if not await box.count():
            return False
        await self._click(box.first)
        await human_type(page, city)
        await page.wait_for_timeout(random.randint(1_500, 2_200))
        options = page.locator(_SUGGESTION)
        for i in range(min(await options.count(), 8)):
            text = (await options.nth(i).inner_text()).strip().split("\n")[0].strip().lower()
            if text.startswith(city.strip().lower()):
                await self._click(options.nth(i))
                await page.wait_for_timeout(random.randint(500, 900))
                return True
        await page.keyboard.press("Escape")
        await box.first.fill("")
        return False

    async def _read_results(self) -> bool:
        """Scroll through the result list and dwell a random
        resdex_results_dwell_min_s..max_s like a recruiter skimming it. Never
        clicks a candidate. Best-effort; True when a dwell happened."""
        from app.config import get_settings
        from app.scrapers.human import human_read_dwell

        s = get_settings()
        lo = max(0.0, float(s.resdex_results_dwell_min_s))
        hi = max(lo, float(s.resdex_results_dwell_max_s))
        total = random.uniform(lo, hi)
        if total <= 0:
            return False
        try:
            await human_read_dwell(self._page, total, rounds=random.randint(2, 4))
        except Exception as exc:  # noqa: BLE001 — pacing must never fail a search
            logger.debug("resdex_results_dwell_failed", error=str(exc)[:200])
        return True

    async def search(self, tier: ResdexTier) -> tuple[list[ResdexCard], int, dict[str, Any]]:
        """Run one search. Returns (cards, total_results, applied) where `applied`
        records which filters actually made it into the form."""
        page = self._page
        f = tier.filters or {}
        await self._open_form()
        applied: dict[str, Any] = {"boolean": await self._boolean_on()}
        box = _BOOLEAN_BOX if applied["boolean"] else _KEYWORD_BOX
        if not await self._fill(box, tier.query):
            raise RuntimeError("Resdex keyword box not found")
        if not applied["boolean"]:
            # Chip mode: Enter commits the typed text as a keyword chip.
            await page.keyboard.press("Enter")
        if f.get("min_exp") is not None and await self._fill('input[name="minExp"]', str(f["min_exp"])):
            applied["min_exp"] = f["min_exp"]
            await page.keyboard.press("Escape")
        if f.get("max_exp") is not None and await self._fill('input[name="maxExp"]', str(f["max_exp"])):
            applied["max_exp"] = f["max_exp"]
            await page.keyboard.press("Escape")
        added = [c for c in (f.get("locations") or [])[:_MAX_LOCATIONS] if await self._add_location(c)]
        if added:
            applied["locations"] = added

        try:
            async with page.expect_response(
                lambda r: _SEARCH_API in r.url and r.request.method == "POST", timeout=60_000,
            ) as resp_info:
                await self._click(page.locator(_SEARCH_BTN).first)
            resp = await resp_info.value
        except Exception:
            await self._guard()   # a wall / login instead of the results call
            raise
        status = getattr(resp, "status", 200)
        if status in (401, 403, 429):
            await self._guard()
            raise RuntimeError(f"Resdex search API answered HTTP {status}")
        payload: dict[str, Any] = {}
        try:
            payload = await resp.json()
        except Exception as exc:  # noqa: BLE001 — fall back to the rendered cards
            logger.warning("resdex_results_json_unreadable", error=str(exc)[:200])

        links: dict[str, str] = {}
        try:
            await page.locator(".tuple[data-tuple-id]").first.wait_for(state="attached", timeout=20_000)
            links = await page.evaluate(_LINKS_JS)
        except Exception:  # noqa: BLE001 — zero results renders no tuples
            pass
        await self._guard()   # a wall rendered over / instead of the results
        if await self._read_results():
            # Scrolling may render more tuples (lazy list) — add their links.
            try:
                more = await page.evaluate(_LINKS_JS)
                for uid, href in (more or {}).items():
                    links.setdefault(uid, href)
            except Exception:  # noqa: BLE001
                pass

        months = int(f.get("active_within_months") or 0)
        if payload.get("tuples") is not None:
            cards = parse_resdex_json(payload, links, active_within_months=months)
            return cards, int(payload.get("totalResumes") or len(cards)), applied

        # JSON unavailable — hand the rendered cards' text to the LLM extractor.
        rendered = await page.evaluate(_TEXT_CARDS_JS) if links else []
        cards = []
        for index, c in enumerate(rendered):
            if not c.get("text"):
                continue
            # The /v3/preview href is session-bound — keep only its search id and
            # result position, never the link itself.
            sid, href_index = preview_link_context(c.get("href") or "")
            cards.append(ResdexCard(
                method="text", text=c["text"], profile_url="",
                raw={"jsUserId": c.get("id"), "sid": sid or None,
                     "tupleIndex": href_index if href_index is not None else index,
                     "verificationStatus": c.get("status") or ""}))
        return cards, len(cards), applied


async def _pause(seconds: float) -> None:
    """asyncio.sleep indirection for the batch pacing (tests patch it to zero)."""
    if seconds > 0:
        await asyncio.sleep(seconds)


class _BatchAbort(Exception):
    """Ends the batch: the current job (unless it already has cards) and every
    remaining job get `status` / `message`."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


_SESSION_EXPIRED_MSG = "Naukri recruiter session expired"
_STOP_MSG = "harvest stop requested"


async def run_resdex_batch(
    jobs: list[ResdexJobQuery], *, headless: bool = False, min_results: int = 2,
    reserve_search: Callable[[], Awaitable[bool]] | None = None,
    on_human_needed: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, ResdexBatchResult]:
    """Search Resdex for every job, one after another, in ONE recruiter-browser
    session. Per job the relaxation tiers are tried in order until a tier yields
    ≥ `min_results` cards (each submitted search reserves one unit of the daily
    search cap first — via `reserve_search`, default resdex_budget.try_reserve_search;
    callers on another event loop pass a marshalled one). Never raises — every
    job gets a ResdexBatchResult.

    Pacing: the RESDEX_DELAY_* gap between searches, plus every
    resdex_break_every_searches searches a resdex_break_min_s..max_s break — both
    end early on a harvest stop. A CAPTCHA / bot wall calls `on_human_needed(reason)`
    once per detection (default None = log only), then waits up to
    resdex_human_wait_s for a person to clear it in the Live Browser and retries
    the search; unsolved → this and the remaining jobs are `error`. A redirect to
    a recruiter login page → `session_expired` for the rest (no waiting here)."""
    from app.agents.naukri_recruiter_agent import (
        check_recruiter_session,
        login_window_open,
        recruiter_harvest_browser,
    )
    from app.config import get_settings
    from app.services.resdex_budget import try_reserve_search
    from app.services.run_guard import is_stop_requested

    settings = get_settings()
    reserve = reserve_search or try_reserve_search
    results: dict[str, ResdexBatchResult] = {}

    def _rest(status: str, error: str | None = None) -> None:
        for j in jobs:
            results.setdefault(j.job_url, ResdexBatchResult(status=status, error=error))

    if not jobs:
        return results
    if login_window_open():
        _rest(ResdexSearchStatus.SESSION_BUSY, "Naukri recruiter login window is open")
        return results

    async def _sleep_or_stop(seconds: float) -> None:
        """Sleep in ≤ _HUMAN_POLL_S chunks; _BatchAbort as soon as a stop is requested."""
        left = max(0.0, float(seconds))
        while left > 0:
            if is_stop_requested():
                raise _BatchAbort(ResdexSearchStatus.ERROR, _STOP_MSG)
            chunk = min(_HUMAN_POLL_S, left)
            await _pause(chunk)
            left -= chunk
        if is_stop_requested():
            raise _BatchAbort(ResdexSearchStatus.ERROR, _STOP_MSG)

    async def _pace(searches_done: int) -> None:
        """Gap before every search but the first, plus the periodic long break."""
        if searches_done <= 0:
            return
        lo = max(0, int(settings.resdex_delay_min_ms))
        hi = max(lo, int(settings.resdex_delay_max_ms))
        await _sleep_or_stop(random.randint(lo, hi) / 1000)
        every = int(settings.resdex_break_every_searches or 0)
        if every > 0 and searches_done % every == 0:
            b_lo = max(0.0, float(settings.resdex_break_min_s))
            b_hi = max(b_lo, float(settings.resdex_break_max_s))
            pause_s = random.uniform(b_lo, b_hi)
            logger.info("resdex_break", after_searches=searches_done, break_s=int(pause_s))
            await _sleep_or_stop(pause_s)

    async def _await_human(page: Any, reason: str) -> None:
        """Hold a challenge in the Live Browser for a person (never solved here).
        Returns once two polls in a row see a normal page; _BatchAbort on timeout,
        stop request, or a login redirect."""
        limit = max(0, int(settings.resdex_human_wait_s or 0))
        logger.warning(
            "resdex_challenge_detected", reason=reason, wait_s=limit,
            url=str(getattr(page, "url", "") or "").split("?")[0],
            hint="Resdex needs you — solve the challenge in the Live Browser",
        )
        if on_human_needed is not None:
            try:
                await on_human_needed(reason)
            except Exception as exc:  # noqa: BLE001 — a notifier must not end the batch
                logger.warning("resdex_human_needed_callback_failed", error=str(exc)[:200])
        try:
            await page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        waited = 0.0
        clean = 0
        while waited < limit:
            if is_stop_requested():
                raise _BatchAbort(ResdexSearchStatus.ERROR, _STOP_MSG)
            chunk = min(_HUMAN_POLL_S, limit - waited)
            await _pause(chunk)
            waited += chunk
            if _on_login_page(page):
                raise _BatchAbort(ResdexSearchStatus.SESSION_EXPIRED, _SESSION_EXPIRED_MSG)
            # Two clean polls in a row, so a blank mid-navigation frame isn't
            # mistaken for a solved challenge.
            if await detect_challenge(page) is None:
                clean += 1
                if clean >= 2:
                    logger.info("resdex_challenge_cleared", waited_s=int(waited))
                    return
            else:
                clean = 0
        logger.warning("resdex_challenge_timeout", waited_s=int(waited), reason=reason)
        if limit <= 0:
            raise _BatchAbort(ResdexSearchStatus.ERROR,
                              "Resdex challenge detected (RESDEX_HUMAN_WAIT_S=0, no human wait)")
        raise _BatchAbort(ResdexSearchStatus.ERROR,
                          f"Resdex challenge not solved within {max(1, math.ceil(limit / 60))} min")

    async def _search_tier(agent: ResdexCandidateAgent, page: Any, tier: ResdexTier):
        """agent.search with the challenge / login handling; a human-cleared
        challenge retries the same search (same budget reservation)."""
        challenges = 0
        while True:
            try:
                return await agent.search(tier)
            except ResdexSessionExpired as exc:
                logger.warning("resdex_session_expired_mid_batch", landed=str(exc)[:200])
                raise _BatchAbort(ResdexSearchStatus.SESSION_EXPIRED, _SESSION_EXPIRED_MSG) from None
            except ResdexChallenge as exc:
                challenges += 1
                if challenges > _MAX_CHALLENGE_RETRIES:
                    raise _BatchAbort(ResdexSearchStatus.ERROR,
                                      "Resdex challenge keeps reappearing") from None
                await _await_human(page, str(exc))

    try:
        async with recruiter_harvest_browser(headless=headless) as pbm:
            page = await pbm.acquire_page(label="resdex")
            ok, landed = await check_recruiter_session(page)
            if not ok:
                # check_recruiter_session returns "<reason>" (not a URL) when the check
                # itself failed — DNS / network / timeout — which is NOT an expiry.
                if not landed.startswith("http"):
                    logger.warning("resdex_session_check_failed", reason=landed[:200])
                    _rest(ResdexSearchStatus.ERROR, landed[:300])
                    return results
                logger.warning("resdex_session_expired", landed=landed.split("?")[0])
                _rest(ResdexSearchStatus.SESSION_EXPIRED, _SESSION_EXPIRED_MSG)
                return results
            agent = ResdexCandidateAgent(page)
            failures = 0
            searches_done = 0
            for job in jobs:
                if is_stop_requested():
                    _rest(ResdexSearchStatus.ERROR, _STOP_MSG)
                    break
                # `best` = the tier with the most cards so far (a relaxed tier that
                # returns fewer must not discard an earlier tier's candidates);
                # `last` = the outcome to report when no tier produced any card.
                best: ResdexBatchResult | None = None
                last = ResdexBatchResult(status=ResdexSearchStatus.NO_RESULTS)
                used = 0
                capped = False
                abort: _BatchAbort | None = None
                try:
                    for tier in job.tiers[: max(1, settings.resdex_max_searches_per_job)]:
                        # Pace BEFORE reserving, so a stop during a gap/break
                        # never spends a unit of the daily search cap.
                        await _pace(searches_done)
                        if not await reserve():
                            capped = True
                            break
                        searches_done += 1
                        used += 1
                        try:
                            cards, total, applied = await _search_tier(agent, page, tier)
                            failures = 0
                        except _BatchAbort:
                            raise
                        except Exception as exc:  # noqa: BLE001
                            if _on_login_page(page):
                                raise _BatchAbort(ResdexSearchStatus.SESSION_EXPIRED,
                                                  _SESSION_EXPIRED_MSG) from None
                            failures += 1
                            logger.warning("resdex_search_failed", job_url=job.job_url,
                                           tier=tier.tier, error=str(exc)[:300])
                            last = ResdexBatchResult(status=ResdexSearchStatus.ERROR, tier=tier.tier,
                                                     query=tier.query, filters=tier.filters,
                                                     error=str(exc)[:300])
                            if failures >= 3:
                                break
                            continue
                        last = ResdexBatchResult(
                            status=ResdexSearchStatus.MATCHED if cards else ResdexSearchStatus.NO_RESULTS,
                            tier=tier.tier, query=tier.query,
                            filters={**tier.filters, "applied": applied}, result_count=total, cards=cards,
                        )
                        logger.info("resdex_search_done", job_url=job.job_url, tier=tier.tier,
                                    total=total, cards=len(cards), boolean=applied.get("boolean"))
                        if cards and (best is None or len(cards) > len(best.cards)):
                            best = last
                        if len(cards) >= min_results:
                            break
                except _BatchAbort as exc:
                    abort = exc
                res = best or last
                if abort is not None and not res.cards:
                    # The ladder did not finish — report why, not a misleading no_results.
                    res = ResdexBatchResult(status=abort.status, error=abort.message)
                if capped and not res.cards:
                    res = ResdexBatchResult(status=ResdexSearchStatus.CAP_REACHED,
                                            error="daily Resdex search cap reached")
                res.searches_used = used
                results[job.job_url] = res
                if abort is not None:
                    logger.warning("resdex_batch_aborted", status=abort.status, reason=abort.message,
                                   job_url=job.job_url)
                    _rest(abort.status, abort.message)
                    break
                if capped:
                    _rest(ResdexSearchStatus.CAP_REACHED, "daily Resdex search cap reached")
                    break
                if failures >= 3:
                    _rest(ResdexSearchStatus.ERROR, "repeated Resdex search failures")
                    break
    except Exception as exc:  # noqa: BLE001 — browser launch / profile lock failure
        logger.warning("resdex_batch_failed", error=str(exc)[:300])
        _rest(ResdexSearchStatus.ERROR, str(exc)[:300])
    return results
