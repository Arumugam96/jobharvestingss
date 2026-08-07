"""
Naukri Harvest Agent API routes.

Visible endpoints (Swagger)
───────────────────────────
  POST   /run-naukri-agent          trigger a Naukri harvest run now
  POST   /naukri-setup-session      open Chrome profile for one-time manual login
  GET    /naukri-results            list all saved Naukri result files
  GET    /naukri-results/{run_id}   retrieve one saved result
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse

from app.agents.naukri_agent import NaukriAgent, NaukriScrapedJob
from app.core.proactor import needs_proactor, run_in_proactor
from app.models.harvest_models import FiltersConfig
from app.models.harvest_run import HarvestRunORM, ScrapedJobORM
from app.models.response_models import NaukriJob, NaukriRunResponse
from app.services.config_service import ConfigService
from app.services.harvest_run_service import (
    HarvestRunService,
    db_read,
    db_write,
    filters_view,
    run_to_result_summary,
)
from app.services.naukri_storage_service import NaukriStorageService

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Naukri Harvest Agent"])

_config_svc  = ConfigService()
_storage_svc = NaukriStorageService()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_run_id(keyword: str, location: str) -> str:
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "_", f"{keyword} {location}".lower()).strip("_")
    return f"{ts}_naukri_{slug[:30]}"


def _err(msg: str, reason: str = "") -> JSONResponse:
    body: dict[str, Any] = {"status": "failed", "message": msg}
    if reason:
        body["reason"] = reason
    return JSONResponse(status_code=200, content=body)


def _to_naukri_job(j: NaukriScrapedJob) -> NaukriJob:
    return NaukriJob(
        job_title       = j.job_title,
        company         = j.company,
        location        = j.location,
        salary          = j.salary,
        experience      = j.experience,
        posted_date     = j.posted_date,
        job_url         = j.job_url,
        job_description = j.job_description,
        skills          = j.skills,
        source          = "Naukri",
    )


def _build_payload(
    run_id:     str,
    executed_at: str,
    f:          FiltersConfig,
    response:   NaukriRunResponse,
) -> dict:
    return {
        "run_id":      run_id,
        "executed_at": executed_at,
        "status":      response.status,
        "source":      "Naukri",
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


def _to_scraped_job_dict(j: NaukriScrapedJob) -> dict[str, Any]:
    """NaukriScrapedJob -> ScrapedJobORM's canonical dict shape. Naukri's own
    dataclass names recruiter fields differently from LinkedIn's (recruiter_name/
    recruiter_company vs. job_poster_name/job_poster_company) — translate here."""
    return {
        "source":                 "Naukri",
        "job_title":              j.job_title,
        "company":                j.company,
        "location":               j.location,
        "salary":                 j.salary,
        "experience":             j.experience,
        "posted_date":            j.posted_date,
        "job_url":                j.job_url,
        "job_description":        j.job_description,
        "skills":                 j.skills,
        "work_mode":              j.work_mode,
        "job_poster_name":        j.recruiter_name,
        "job_poster_designation": j.job_poster_designation,
        "current_company":        j.recruiter_company,
        "email_id":               j.email_id,
        "contact_number":         j.contact_number,
    }


def _scraped_job_to_naukri_dict(j: ScrapedJobORM) -> dict[str, Any]:
    """ScrapedJobORM -> the exact dict shape NaukriJob.model_dump() produces."""
    return {
        "job_title":       j.job_title,
        "company":         j.company,
        "location":        j.location,
        "salary":          j.salary,
        "experience":      j.experience,
        "posted_date":     j.posted_date,
        "job_url":         j.job_url,
        "job_description": j.job_description,
        "skills":          j.skills,
        "source":          j.source,
    }


def _run_to_naukri_payload(run: HarvestRunORM) -> dict[str, Any]:
    """HarvestRunORM (+ its ScrapedJobORM rows) -> the exact payload shape
    _build_payload()/_storage_svc.save_results() already produces."""
    return {
        "run_id":      run.run_id,
        "executed_at": run.started_at.isoformat() if run.started_at else "",
        "status":      run.status,
        "source":      "Naukri",
        "total_found": run.combined_count,
        "filters":     filters_view(run.filters_snapshot),
        "jobs":        [_scraped_job_to_naukri_dict(j) for j in run.jobs],
    }


# ══════════════════════════════════════════════════════════════════════════════
# POST /run-naukri-agent
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/run-naukri-agent", status_code=status.HTTP_200_OK)
async def run_naukri_agent() -> Any:
    """
    Trigger a Naukri.com harvest run using the current harvest_config.json settings.
    Edit search filters via PUT /harvest-config or the Rule Engine UI.
    Returns: run_id, status, source, total_found, saved_to, jobs.
    """
    config = _config_svc.load()
    f      = config.filters

    run_id  = _make_run_id(f.keyword, f.location)
    now_iso = datetime.now(timezone.utc).isoformat()

    log = logger.bind(run_id=run_id, keyword=f.keyword, location=f.location)
    log.info("naukri_search_started", max_jobs=f.max_jobs)

    # ── Playwright scrape via proactor thread on Windows --reload ─────────────
    async def _do_harvest() -> list[NaukriScrapedJob]:
        agent = NaukriAgent()
        return await agent.harvest(
            filters  = f,
            headless = config.browser.resolved_headless,
            slow_mo  = config.browser.slow_mo_ms,
        )

    try:
        if needs_proactor():
            log.debug("using_proactor_thread")
            scraped: list[NaukriScrapedJob] = await run_in_proactor(_do_harvest)
        else:
            scraped = await _do_harvest()

    except RuntimeError as exc:
        err_lower = str(exc).lower()
        if "login" in err_lower or "failed" in err_lower:
            log.error("naukri_login_failed", error=str(exc))
            return _err("Naukri login failed", "naukri_login_failed")
        log.exception("naukri_harvest_error", error=str(exc))
        return _err("Naukri harvest failed", str(exc))

    except Exception as exc:
        log.exception("naukri_harvest_error", error=str(exc))
        return _err("Naukri harvest failed", str(exc) or "Unexpected error during scraping")

    log.info("naukri_jobs_extracted", total=len(scraped))

    jobs = [_to_naukri_job(j) for j in scraped]

    async def _mirror_run_to_db(response: NaukriRunResponse) -> None:
        run_pk = await db_write(lambda db: HarvestRunService(db).create_run(
            run_id           = run_id,
            source           = "Naukri",
            sources          = ["Naukri"],
            filters_snapshot = f.model_dump(),
            started_at       = datetime.now(timezone.utc),
        ))
        if not run_pk:
            return
        await db_write(lambda db: HarvestRunService(db).update_run(
            run_pk,
            status         = response.status,
            completed_at   = datetime.now(timezone.utc),
            json_path      = response.saved_to or None,
            combined_count = response.total_found,
        ))
        await db_write(lambda db: HarvestRunService(db).bulk_insert_scraped_jobs(
            run_pk, [_to_scraped_job_dict(j) for j in scraped],
        ))

    # ── No results ────────────────────────────────────────────────────────────
    if not jobs:
        response = NaukriRunResponse(
            run_id      = run_id,
            status      = "no_results",
            source      = "Naukri",
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
        await _mirror_run_to_db(response)
        return response.model_dump()

    # ── Success ───────────────────────────────────────────────────────────────
    response = NaukriRunResponse(
        run_id      = run_id,
        status      = "success",
        source      = "Naukri",
        total_found = len(jobs),
        executed_at = now_iso,
        saved_to    = "",
        jobs        = jobs,
    )

    try:
        payload = _build_payload(run_id, now_iso, f, response)
        response.saved_to = _storage_svc.save_results(payload)
        log.info("naukri_results_saved", saved_to=response.saved_to)
    except Exception as exc:
        log.warning("naukri_save_failed", error=str(exc))

    await _mirror_run_to_db(response)
    return response.model_dump()


# ══════════════════════════════════════════════════════════════════════════════
# POST /naukri-setup-session
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/naukri-setup-session", status_code=status.HTTP_200_OK)
async def setup_naukri_session() -> Any:
    """
    Opens Chrome with the dedicated harvest agent profile directory.
    Log in to Naukri / recruit.naukri.com manually in the browser window that appears.
    Close the browser when done — the session is persisted in the profile
    directory and all future /run-naukri-agent calls will reuse it.

    Profile directory: data/chrome_profile (configurable in harvest_config.json)
    Times out after 10 minutes.
    """
    from app.scrapers.browser_manager import PersistentBrowserManager

    config         = _config_svc.load()
    chrome_profile = config.browser.chrome_profile

    async def _open_for_login() -> str:
        import os
        naukri_email    = os.environ.get("NAUKRI_EMAIL", "")
        naukri_password = os.environ.get("NAUKRI_PASSWORD", "")

        async with PersistentBrowserManager(
            profile_dir = chrome_profile,
            headless    = False,
        ) as pbm:
            page = await pbm.new_page()
            await page.goto(
                "https://recruit.naukri.com/",
                wait_until = "domcontentloaded",
                timeout    = 30_000,
            )
            await page.wait_for_timeout(2_000)

            _LOGIN_PATHS = ("/recruit/login", "/nlogin/login", "/nlogin/")
            current_url  = page.url

            # Auto-fill credentials if on a login page and creds are available
            if naukri_email and naukri_password and any(p in current_url for p in _LOGIN_PATHS):
                logger.info(
                    "naukri_setup_autofill",
                    email   = naukri_email,
                    profile = chrome_profile,
                )
                _EMAIL_SELS = [
                    'input[type="email"]',
                    'input[name="username"]',
                    'input[name="emailId"]',
                    'input[id*="email" i]',
                    'input[placeholder*="email" i]',
                    'input[placeholder*="username" i]',
                ]
                _PASS_SELS = [
                    'input[type="password"]',
                    'input[name="password"]',
                    'input[id*="password" i]',
                    'input[placeholder*="password" i]',
                ]
                _SUBMIT_SELS = [
                    'button[type="submit"]',
                    'input[type="submit"]',
                    'button:has-text("Login")',
                    'button:has-text("Sign in")',
                    'button:has-text("Log in")',
                    '.loginBtn',
                    '[class*="loginBtn"]',
                ]

                # Fill email
                for sel in _EMAIL_SELS:
                    try:
                        el = page.locator(sel).first
                        if await el.count() > 0:
                            await el.click()
                            await el.fill(naukri_email)
                            logger.info("naukri_autofill_email_entered")
                            break
                    except Exception:
                        continue

                # Fill password
                for sel in _PASS_SELS:
                    try:
                        el = page.locator(sel).first
                        if await el.count() > 0:
                            await el.click()
                            await el.fill(naukri_password)
                            logger.info("naukri_autofill_password_entered")
                            break
                    except Exception:
                        continue

                # Click submit
                for sel in _SUBMIT_SELS:
                    try:
                        btn = page.locator(sel).first
                        if await btn.count() > 0:
                            await btn.click()
                            logger.info("naukri_autofill_submitted")
                            await page.wait_for_timeout(4_000)
                            break
                    except Exception:
                        continue
            else:
                logger.info(
                    "naukri_setup_browser_opened",
                    msg     = "Naukri login page opened — waiting for manual login or already authenticated.",
                    profile = chrome_profile,
                )

            # Wait for navigation away from login (up to 10 min for manual completion)
            for _ in range(300):   # 300 × 2 s = 10 min
                await page.wait_for_timeout(2_000)
                url = page.url
                if not any(p in url for p in _LOGIN_PATHS):
                    logger.info("naukri_setup_login_detected", url=url)
                    break
            else:
                raise RuntimeError("Setup timed out — login not completed within 10 minutes")

        return chrome_profile

    try:
        if needs_proactor():
            profile: str = await run_in_proactor(_open_for_login)
        else:
            profile = await _open_for_login()
        return {
            "status":   "ready",
            "message":  "Naukri session saved in Chrome profile. Future /run-naukri-agent calls will reuse it.",
            "profile":  profile,
        }
    except Exception as exc:
        logger.error("naukri_setup_session_failed", error=str(exc))
        return _err("Failed to set up Naukri session", str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# POST /naukri-extract-session
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/naukri-start-cdp-browser", status_code=status.HTTP_200_OK)
async def naukri_start_cdp_browser() -> Any:
    """
    Step 1 of Naukri session capture:
    Kills any existing Chrome and starts a fresh Chrome with --remote-debugging-port=9222.

    After this returns, click "Open in Browser" in the Naukri Recruiter Launcher.
    The Launcher's auto-login URL will open as a new tab in this CDP-enabled Chrome.
    Wait ~5 seconds, then call POST /naukri-extract-session to capture the session.
    """
    try:
        from app.services.chrome_session_extractor import start_cdp_chrome
        result = start_cdp_chrome()
        return result
    except Exception as exc:
        logger.error("naukri_start_cdp_browser_failed", error=str(exc))
        return {"status": "failed", "message": str(exc)}


@router.post("/naukri-extract-session", status_code=status.HTTP_200_OK)
async def naukri_extract_session() -> Any:
    """
    Extract Naukri authentication cookies from the user's Chrome browser via CDP.

    How it works:
    - If Chrome is running with --remote-debugging-port=9222: connects and extracts cookies.
    - If Chrome is NOT running with CDP: starts Chrome with CDP automatically.
    - Navigates to recruit.naukri.com to verify login status.
    - If logged in: saves cookies to data/sessions/naukri_session.json and returns "ready".
    - If NOT logged in: returns "action_required" — Chrome window is open, log in there.

    After calling this once when logged in, future /run-lead-intelligence calls reuse the
    saved session file automatically.
    """
    from app.services.chrome_session_extractor import extract_naukri_session
    from app.core.proactor import needs_proactor, run_in_proactor

    async def _extract() -> dict:
        return await extract_naukri_session()

    try:
        if needs_proactor():
            result: dict = await run_in_proactor(_extract)
        else:
            result = await _extract()

        logger.info(
            "naukri_extract_session_result",
            status       = result.get("status"),
            logged_in    = result.get("logged_in"),
            cookies_found= result.get("cookies_found"),
        )
        return result

    except Exception as exc:
        err_msg = str(exc)
        logger.error("naukri_extract_session_failed", error=err_msg)
        return {
            "status":    "failed",
            "message":   "Authenticated Chrome profile not detected",
            "reason":    err_msg,
            "next_step": [
                "Make sure Chrome is installed at the standard path.",
                "Call POST /naukri-extract-session again.",
            ],
        }


# ══════════════════════════════════════════════════════════════════════════════
# GET /naukri-results
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/naukri-results", status_code=status.HTTP_200_OK)
async def list_naukri_results() -> Any:
    """List all saved Naukri harvest run files, newest first."""
    runs = await db_read(lambda db: HarvestRunService(db).list_runs(source="Naukri"))
    if runs:
        results = [run_to_result_summary(r) for r in runs]
        return {"total_runs": len(results), "results": results}

    results = _storage_svc.list_results()
    return {"total_runs": len(results), "results": results}


# ══════════════════════════════════════════════════════════════════════════════
# GET /naukri-results/{run_id}
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/naukri-results/{run_id}", status_code=status.HTTP_200_OK)
async def get_naukri_result(run_id: str) -> Any:
    """Return the full JSON payload for a single saved Naukri run."""
    run = await db_read(lambda db: HarvestRunService(db).get_by_run_id(run_id, source="Naukri"))
    if run is not None:
        return _run_to_naukri_payload(run)

    data = _storage_svc.get_result(run_id)
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No Naukri result found for run_id '{run_id}'",
        )
    return data
