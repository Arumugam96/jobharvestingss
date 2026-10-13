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
from app.services import run_guard

from app.models.harvest_run import HarvestRunORM, ScrapedJobORM
from app.models.response_models import NaukriJob, NaukriRunResponse
from app.services.config_service import ConfigService
from app.services.harvest_run_service import (
    HarvestRunService,
    data_source_mode,
    db_read,
    db_write,
    filters_view,
    resolve_read,
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
        "job_description_html":   j.job_description_html,
        "skills":                 j.skills,
        "work_mode":              j.work_mode,
        "company_url":            j.company_url,
        "company_size":           j.company_size,
        "employment_type":        j.employment_type,
        "job_type":               j.employment_type,
        "job_poster_name":        j.recruiter_name,
        "job_poster_designation": j.job_poster_designation,
        "linkedin_profile_url":   j.linkedin_profile_url,
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

    # Single-flight: reject if a harvest is already running (raises 409).
    async with run_guard.single_flight(run_id, "Naukri"):
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

    # Results are persisted to the database only (see _mirror_run_to_db) —
    # no per-run JSON file is written anymore.
    log.info("naukri_jobs_saved", count=len(jobs))

    await _mirror_run_to_db(response)
    return response.model_dump()


# ══════════════════════════════════════════════════════════════════════════════
# POST /naukri-setup-session
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/naukri-setup-session", status_code=status.HTTP_200_OK)
async def setup_naukri_session() -> Any:
    """
    Connect the Naukri RECRUITER session (the one recruiter-mode harvests run in).

    Delegates to the recruiter session flow (app/agents/naukri_recruiter_agent.py,
    same as POST /naukri-recruiter/login): opens a HEADED window on
    recruit.naukri.com in the DEDICATED recruiter profile
    (settings.naukri_recruiter_profile_dir) — on the server, watch/use it in the
    Live Browser view. Log in yourself and complete any OTP / Naukri Recruiter app
    approval; nothing is typed for you (NAUKRI_EMAIL / NAUKRI_PASSWORD are not
    used) and nothing is written to the LinkedIn harvest profile.

    Blocks until the login is confirmed or 10 minutes pass. Joins the window if
    one is already open.
    """
    from app.agents.naukri_recruiter_agent import (
        read_status,
        recruiter_profile_dir,
        start_login_window,
        wait_for_login_window,
    )

    timeout_s = 600
    started_at = datetime.now(timezone.utc)
    started = start_login_window(timeout_s=timeout_s)
    logger.info("naukri_setup_session_login_window", started=started,
                profile=str(recruiter_profile_dir()))
    try:
        # The window itself gives up after timeout_s; allow for browser launch.
        finished = await wait_for_login_window(timeout_s + 60)
        if not finished:
            return _err("Failed to set up Naukri session",
                        "Setup timed out — login not completed within 10 minutes")
        last = read_status().get("last_check") or {}
        fresh = False
        try:
            fresh = datetime.fromisoformat(str(last.get("checked_at"))) >= started_at
        except (TypeError, ValueError):
            fresh = False
        if fresh and last.get("logged_in"):
            return {
                "status":  "ready",
                "message": "Naukri recruiter session saved.",
                "profile": str(recruiter_profile_dir()),
            }
        if last.get("state") == "error":
            reason = last.get("error") or "Recruiter login window failed"
        elif last.get("state") == "timed_out":
            reason = "Setup timed out — login not completed within 10 minutes"
        else:
            reason = "Login was not completed in the recruiter window"
        return _err("Failed to set up Naukri session", str(reason))
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
    mode = data_source_mode()
    runs, source = await resolve_read(
        mode,
        lambda: db_read(lambda db: HarvestRunService(db).list_runs(source="Naukri")),
        lambda: _storage_svc.list_results(),
    )
    if source == "database":
        results = [run_to_result_summary(r) for r in runs] if runs else []
        return {"total_runs": len(results), "results": results}

    return {"total_runs": len(runs), "results": runs}


# ══════════════════════════════════════════════════════════════════════════════
# GET /naukri-results/{run_id}
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/naukri-results/{run_id}", status_code=status.HTTP_200_OK)
async def get_naukri_result(run_id: str) -> Any:
    """Return the full JSON payload for a single saved Naukri run."""
    mode = data_source_mode()
    run, source = await resolve_read(
        mode,
        lambda: db_read(lambda db: HarvestRunService(db).get_by_run_id(run_id, source="Naukri")),
        lambda: _storage_svc.get_result(run_id),
    )
    if source == "database":
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No Naukri result found for run_id '{run_id}'",
            )
        return _run_to_naukri_payload(run)

    data = run
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No Naukri result found for run_id '{run_id}'",
        )
    return data
