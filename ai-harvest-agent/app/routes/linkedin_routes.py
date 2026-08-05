"""
LinkedIn Harvest Agent API routes.

Visible endpoints (Swagger)
───────────────────────────
  POST   /run-linkedin-agent             trigger a LinkedIn harvest run now
  POST   /linkedin-setup-session         open Chrome profile for one-time manual login
  GET    /linkedin-auth-status           check current LinkedIn session state (non-destructive)
  GET    /linkedin-results               list all saved LinkedIn result files
  GET    /linkedin-results/{run_id}      retrieve one saved result
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse

from app.agents.linkedin_agent import (
    LinkedInAgent,
    LinkedInLoginError,
    LinkedInScrapedJob,
)
from app.core.proactor import needs_proactor, run_in_proactor
from app.models.harvest_models import FiltersConfig
from app.models.response_models import LinkedInJob, LinkedInRunResponse
from app.services.config_service import ConfigService
from app.services.linkedin_storage_service import LinkedInStorageService

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["LinkedIn Harvest Agent"])

# ── Auth-state detection constants ────────────────────────────────────────────

_GATED     = ("/login", "/checkpoint", "/challenge", "/authwall", "/uas/")
_MFA_PATHS = ("/checkpoint", "/challenge")


def _detect_auth_state(url: str) -> str:
    """Return a structured auth state string based on the current browser URL."""
    if any(p in url for p in _MFA_PATHS):
        return "mfa_required"
    if any(p in url for p in ("/login", "/authwall", "/uas/")):
        return "login_required"
    if "linkedin.com" in url:
        return "logged_in"
    return "unknown"

_config_svc  = ConfigService()
_storage_svc = LinkedInStorageService()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_run_id(keyword: str, location: str) -> str:
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "_", f"{keyword} {location}".lower()).strip("_")
    return f"{ts}_linkedin_{slug[:30]}"


def _err(msg: str, reason: str = "") -> JSONResponse:
    body: dict[str, Any] = {"status": "failed", "message": msg}
    if reason:
        body["reason"] = reason
    return JSONResponse(status_code=200, content=body)


def _to_linkedin_job(j: LinkedInScrapedJob) -> LinkedInJob:
    return LinkedInJob(
        job_title       = j.job_title,
        company         = j.company,
        location        = j.location,
        salary          = j.salary,
        experience      = j.experience,
        posted_date     = j.posted_date,
        job_url         = j.job_url,
        job_description = j.job_description,
        skills          = j.skills,
        work_mode       = j.work_mode,
        company_url     = j.company_url,
        employment_type = j.employment_type,
        source          = "LinkedIn",
        job_poster_name         = j.job_poster_name,
        job_poster_designation  = j.job_poster_designation,
        linkedin_profile_url    = j.linkedin_profile_url,
    )


def _build_payload(
    run_id:      str,
    executed_at: str,
    f:           FiltersConfig,
    response:    LinkedInRunResponse,
) -> dict:
    return {
        "run_id":      run_id,
        "executed_at": executed_at,
        "status":      response.status,
        "source":      "LinkedIn",
        "total_found": response.total_found,
        "filters": {
            "keyword":                  f.keyword,
            "location":                 f.location,
            "job_type":                 f.job_type,
            "work_mode":                f.work_mode,
            "search_window_hours":      f.search_window_hours,
            "max_jobs":                 f.max_jobs,
            "domain":                   f.domain,
            "hiring_entity":            f.hiring_entity,
            "gcc_mode":                 f.gcc_mode,
            "salary_min":               f.salary_min,
            "salary_max":               f.salary_max,
            "salary_currency":          f.salary_currency,
            "include_undisclosed_salary": f.include_undisclosed_salary,
        },
        "jobs": [j.model_dump() for j in response.jobs],
    }


# ══════════════════════════════════════════════════════════════════════════════
# POST /run-linkedin-agent
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/run-linkedin-agent", status_code=status.HTTP_200_OK)
async def run_linkedin_agent() -> Any:
    """
    Trigger a LinkedIn harvest run using the current harvest_config.json settings.
    Edit search filters via PUT /harvest-config or the Rule Engine UI.
    Returns: run_id, status, source, total_found, saved_to, jobs.
    """
    config = _config_svc.load()
    f      = config.filters

    run_id  = _make_run_id(f.keyword, f.location)
    now_iso = datetime.now(timezone.utc).isoformat()

    log = logger.bind(run_id=run_id, keyword=f.keyword, location=f.location)
    log.info("linkedin_search_started", max_jobs=f.max_jobs)

    async def _do_harvest() -> list[LinkedInScrapedJob]:
        agent = LinkedInAgent()
        return await agent.harvest(
            filters  = f,
            headless = config.browser.resolved_headless,
            slow_mo  = config.browser.slow_mo_ms,
        )

    try:
        if needs_proactor():
            log.debug("using_proactor_thread")
            scraped: list[LinkedInScrapedJob] = await run_in_proactor(_do_harvest)
        else:
            scraped = await _do_harvest()

    except LinkedInLoginError as exc:
        log.error("linkedin_login_failed", error=str(exc))
        return _err("LinkedIn login failed — check credentials or run /linkedin-save-session", str(exc))

    except Exception as exc:
        log.exception("linkedin_harvest_error", error=str(exc))
        return _err("LinkedIn harvest failed", str(exc) or "Unexpected error during scraping")

    log.info("linkedin_jobs_extracted", total=len(scraped))

    jobs = [_to_linkedin_job(j) for j in scraped]

    # ── No results ────────────────────────────────────────────────────────────
    if not jobs:
        response = LinkedInRunResponse(
            run_id      = run_id,
            status      = "no_results",
            source      = "LinkedIn",
            total_found = 0,
            executed_at = now_iso,
            saved_to    = "",
            jobs        = [],
        )
        try:
            payload = _build_payload(run_id, now_iso, f, response)
            response.saved_to = _storage_svc.save_results(payload)
        except Exception:
            pass
        return response.model_dump()

    # ── Success ───────────────────────────────────────────────────────────────
    response = LinkedInRunResponse(
        run_id      = run_id,
        status      = "success",
        source      = "LinkedIn",
        total_found = len(jobs),
        executed_at = now_iso,
        saved_to    = "",
        jobs        = jobs,
    )

    try:
        payload = _build_payload(run_id, now_iso, f, response)
        response.saved_to = _storage_svc.save_results(payload)
        log.info("linkedin_jobs_saved", count=len(jobs), saved_to=response.saved_to)
        log.info("linkedin_results_saved", saved_to=response.saved_to)
    except Exception as exc:
        log.warning("linkedin_save_failed", error=str(exc))

    return response.model_dump()


# ══════════════════════════════════════════════════════════════════════════════
# POST /linkedin-setup-session
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/linkedin-setup-session", status_code=status.HTTP_200_OK)
async def setup_linkedin_session() -> Any:
    """
    Opens Chrome with the dedicated harvest agent profile directory.

    **Session reuse**: if the Chrome profile already has a valid LinkedIn session
    the endpoint returns immediately without opening a login page.

    **MFA**: if LinkedIn requests multi-factor authentication, leave the browser
    open, complete MFA in the window, and the session is saved automatically.

    Profile directory: data/chrome_profile (configurable in harvest_config.json)
    Times out after 10 minutes.
    """
    from app.scrapers.browser_manager import PersistentBrowserManager
    from app.services.session_manager import SessionManager

    config         = ConfigService().load()
    chrome_profile = config.browser.chrome_profile

    async def _open_for_login() -> dict:
        from pathlib import Path as _Path

        # ── Step 0: Clear Chrome profile lock files before launching ─────────
        # Prevents "browser has been closed" crash when a previous Playwright
        # instance left the profile locked (e.g. after a headless auth check).
        _profile_path = _Path(chrome_profile)
        for _lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            _lf = _profile_path / _lock
            if _lf.exists():
                try:
                    _lf.unlink()
                    logger.info("chrome_profile_lock_cleared", lock=_lock)
                except Exception:
                    pass

        async with PersistentBrowserManager(
            profile_dir = chrome_profile,
            headless    = False,
        ) as pbm:
            page = await pbm.new_page()

            # ── Step 1: Navigate to LinkedIn home — checks session in ONE pass ─
            # LinkedIn redirects to /feed if already logged in, or to /login if not.
            # This avoids a double-navigation (feed → login) that can cause a race.
            logger.info(
                "linkedin_browser_launched",
                msg     = "Browser launched. Navigating to LinkedIn…",
                profile = chrome_profile,
            )
            await page.goto(
                "https://www.linkedin.com/",
                wait_until = "domcontentloaded",
                timeout    = 30_000,
            )
            await page.wait_for_timeout(3_000)

            state = _detect_auth_state(page.url)

            if state == "logged_in":
                # Verify li_at is actually present — URL alone can be a false positive
                # when LinkedIn serves a cached shell without issuing an auth token.
                ctx_cookies = await pbm.context.cookies()
                li_at_ok    = any(c["name"] == "li_at" for c in ctx_cookies)
                if li_at_ok:
                    logger.info(
                        "linkedin_session_valid",
                        msg = "Session already valid — skipping login page.",
                        url = page.url,
                    )
                    sm = SessionManager("linkedin")
                    await sm.save_session(page)
                    return {
                        "auth_status": "logged_in",
                        "action":      "session_reused",
                        "profile":     chrome_profile,
                    }
                # li_at missing: LinkedIn served cached shell without auth token
                logger.warning(
                    "linkedin_session_false_positive",
                    msg = "URL shows /feed but li_at is absent — session not valid. Forcing fresh login.",
                    url = page.url,
                )
                state = "login_required"

            # ── Step 2: Session expired — navigate to login if not already there
            logger.info(
                "linkedin_session_expired",
                msg = "Session expired — opening LinkedIn login page.",
                url = page.url[:80],
            )
            if state not in ("login_required", "mfa_required"):
                await page.goto(
                    "https://www.linkedin.com/login",
                    wait_until = "domcontentloaded",
                    timeout    = 30_000,
                )
                await page.wait_for_timeout(2_000)
                state = _detect_auth_state(page.url)

            logger.info(
                "linkedin_login_page_opened",
                msg     = "LinkedIn login page is open. Please enter your credentials in the browser window.",
                profile = chrome_profile,
            )

            # ── Step 3: Poll for auth state transitions (10 min) ─────────────
            prev_state = state
            for _ in range(300):   # 300 × 2 s = 10 min
                await page.wait_for_timeout(2_000)
                url   = page.url
                state = _detect_auth_state(url)

                if state != prev_state:
                    if state == "mfa_required":
                        logger.info(
                            "linkedin_mfa_detected",
                            msg         = "LinkedIn requested MFA. Complete authentication in the browser window.",
                            url         = url[:80],
                            next_action = "Enter the code from your authenticator app or SMS.",
                        )
                    elif state == "login_required":
                        logger.info("linkedin_login_page", url=url[:80])
                    elif state == "logged_in":
                        logger.info(
                            "linkedin_login_success",
                            msg = "Authentication successful.",
                            url = url[:80],
                        )
                    prev_state = state

                if state == "logged_in":
                    # Verify li_at is present before accepting the logged_in state
                    ctx_cookies = await pbm.context.cookies()
                    if any(c["name"] == "li_at" for c in ctx_cookies):
                        break
                    # li_at still absent — wait for it to be set
                    state = prev_state
            else:
                raise RuntimeError("Setup timed out — login not completed within 10 minutes")

            # ── Step 4: Persist to Chrome profile (auto) + sessions JSON ────
            sm = SessionManager("linkedin")
            await sm.save_session(page)
            logger.info(
                "linkedin_session_saved",
                profile      = chrome_profile,
                session_file = str(sm.session_path),
                msg          = "Session saved. Future runs will reuse it automatically.",
            )

        return {
            "auth_status": "logged_in",
            "action":      "logged_in_and_saved",
            "profile":     chrome_profile,
        }

    try:
        if needs_proactor():
            result: dict = await run_in_proactor(_open_for_login)
        else:
            result = await _open_for_login()
        return {
            "status":  "ready",
            "message": "LinkedIn session active. Future /run-linkedin-agent calls will reuse it automatically.",
            **result,
        }
    except Exception as exc:
        logger.error("linkedin_setup_session_failed", error=str(exc))
        return _err("Failed to set up LinkedIn session", str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# GET /linkedin-auth-status
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/linkedin-auth-status", status_code=status.HTTP_200_OK)
async def linkedin_auth_status() -> Any:
    """
    Check current LinkedIn session state.

    Uses the saved session file (data/sessions/linkedin_session.json) with a
    non-persistent browser — avoids locking the Chrome profile directory so
    POST /linkedin-setup-session can always launch cleanly.

    Returns one of:
      `{"status": "logged_in"}`        — session is valid, harvesting can start
      `{"status": "login_required"}`   — session expired, call POST /linkedin-setup-session
      `{"status": "mfa_required"}`     — checkpoint page, call POST /linkedin-setup-session
      `{"status": "no_session"}`       — no session file, call POST /linkedin-setup-session
      `{"status": "error", ...}`       — browser failed to launch
    """
    from app.scrapers.browser_manager import BrowserManager
    from app.services.session_manager import SessionManager

    sm = SessionManager("linkedin")

    if not sm.session_exists():
        logger.info("linkedin_auth_status_no_session")
        return {
            "status":  "no_session",
            "message": "No session file found. Call POST /linkedin-setup-session to log in.",
        }

    async def _check() -> dict:
        # Use non-persistent BrowserManager with session file — never locks Chrome profile
        async with BrowserManager(
            headless      = True,
            storage_state = sm.storage_state_arg(),
        ) as bm:
            page = await bm.new_page()
            try:
                await page.goto(
                    "https://www.linkedin.com/feed/",
                    wait_until = "domcontentloaded",
                    timeout    = 25_000,
                )
                await page.wait_for_timeout(2_000)
                url   = page.url
                state = _detect_auth_state(url)
                logger.info("linkedin_auth_status_checked", state=state, url=url[:80])
                result: dict = {"status": state, "url": url}
                if state == "logged_in":
                    result["message"] = "Session valid — harvest agent can start immediately."
                elif state == "mfa_required":
                    result["message"] = "LinkedIn requested MFA. Call POST /linkedin-setup-session and complete MFA in the browser window."
                else:
                    result["message"] = "Session expired. Call POST /linkedin-setup-session to log in."
                return result
            except Exception as exc:
                return {"status": "error", "error": str(exc)}

    try:
        if needs_proactor():
            result: dict = await run_in_proactor(_check)
        else:
            result = await _check()
        return result
    except Exception as exc:
        logger.error("linkedin_auth_status_error", error=str(exc))
        return {"status": "error", "error": str(exc)}


# ══════════════════════════════════════════════════════════════════════════════
# GET /linkedin-results
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/linkedin-results", status_code=status.HTTP_200_OK)
async def list_linkedin_results() -> Any:
    """List all saved LinkedIn harvest run files, newest first."""
    results = _storage_svc.list_results()
    return {"total_runs": len(results), "results": results}


# ══════════════════════════════════════════════════════════════════════════════
# GET /linkedin-results/{run_id}
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/linkedin-results/{run_id}", status_code=status.HTTP_200_OK)
async def get_linkedin_result(run_id: str) -> Any:
    """Return the full JSON payload for a single saved LinkedIn run."""
    data = _storage_svc.get_result(run_id)
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No LinkedIn result found for run_id '{run_id}'",
        )
    return data
