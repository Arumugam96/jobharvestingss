"""
Dice Harvest Agent API routes.

Visible endpoints (Swagger)
───────────────────────────
  POST   /run-dice-agent           trigger a Dice harvest run now
  GET    /dice-results             list all saved Dice result files
  GET    /dice-results/{run_id}    retrieve one saved result
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse

from app.agents.dice_agent import DiceAgent
from app.scrapers.dice_scraper import DiceScrapedJob
from app.core.proactor import needs_proactor, run_in_proactor

from app.models.harvest_run import HarvestRunORM, ScrapedJobORM
from app.models.response_models import DiceJob, DiceRunResponse
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
from app.services.dice_storage_service import DiceStorageService

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Dice Harvest Agent"])

_config_svc  = ConfigService()
_storage_svc = DiceStorageService()


def _make_run_id(keyword: str, location: str) -> str:
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "_", f"{keyword} {location}".lower()).strip("_")
    return f"{ts}_dice_{slug[:30]}"


def _err(msg: str, reason: str = "") -> JSONResponse:
    body: dict[str, Any] = {"status": "failed", "message": msg}
    if reason:
        body["reason"] = reason
    return JSONResponse(status_code=200, content=body)


def _to_dice_job(j: DiceScrapedJob) -> DiceJob:
    return DiceJob(
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
        employment_type = j.employment_type,
        source          = "Dice",
    )


def _to_scraped_job_dict(j: DiceScrapedJob) -> dict[str, Any]:
    """DiceScrapedJob -> ScrapedJobORM's canonical dict shape. Dice's own
    dataclass names recruiter fields differently from LinkedIn's (recruiter_name/
    recruiter_company vs. job_poster_name/job_poster_company) — translate here."""
    return {
        "source":                 "Dice",
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
        "employment_type":        j.employment_type,
        "job_poster_name":        j.recruiter_name,
        "job_poster_designation": j.job_poster_designation,
        "linkedin_profile_url":   j.linkedin_profile_url,
        "current_company":        j.recruiter_company,
        "email_id":               j.email_id,
        "contact_number":         j.contact_number,
    }


def _scraped_job_to_dice_dict(j: ScrapedJobORM) -> dict[str, Any]:
    """ScrapedJobORM -> the exact dict shape DiceJob.model_dump() produces."""
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
        "work_mode":       j.work_mode,
        "employment_type": j.employment_type,
        "source":          j.source,
    }


def _run_to_dice_payload(run: HarvestRunORM) -> dict[str, Any]:
    """HarvestRunORM (+ its ScrapedJobORM rows) -> the exact payload shape
    _build_payload()/_storage_svc.save_results() already produces."""
    return {
        "run_id":      run.run_id,
        "executed_at": run.started_at.isoformat() if run.started_at else "",
        "status":      run.status,
        "source":      "Dice",
        "total_found": run.combined_count,
        "filters":     filters_view(run.filters_snapshot),
        "jobs":        [_scraped_job_to_dice_dict(j) for j in run.jobs],
    }


@router.post("/run-dice-agent", status_code=status.HTTP_200_OK)
async def run_dice_agent() -> Any:
    """
    Trigger a Dice.com harvest run using the current harvest_config.json settings.
    Edit search filters via PUT /harvest-config or the Rule Engine UI.
    Returns: run_id, status, source, total_found, saved_to, jobs.
    """
    config = _config_svc.load()
    f      = config.filters

    run_id  = _make_run_id(f.keyword, f.location)
    now_iso = datetime.now(timezone.utc).isoformat()

    log = logger.bind(run_id=run_id, keyword=f.keyword, location=f.location)
    log.info("dice_search_started", max_jobs=f.max_jobs)

    async def _do_harvest() -> list[DiceScrapedJob]:
        agent = DiceAgent()
        return await agent.harvest(
            filters  = f,
            headless = config.browser.resolved_headless,
            slow_mo  = config.browser.slow_mo_ms,
        )

    try:
        if needs_proactor():
            log.debug("using_proactor_thread")
            scraped: list[DiceScrapedJob] = await run_in_proactor(_do_harvest)
        else:
            scraped = await _do_harvest()

    except Exception as exc:
        log.exception("dice_harvest_error", error=str(exc))
        return _err("Dice harvest failed", str(exc) or "Unexpected error during scraping")

    log.info("dice_jobs_extracted", total=len(scraped))

    jobs = [_to_dice_job(j) for j in scraped]

    async def _mirror_run_to_db(response: DiceRunResponse) -> None:
        run_pk = await db_write(lambda db: HarvestRunService(db).create_run(
            run_id           = run_id,
            source           = "Dice",
            sources          = ["Dice"],
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

    if not jobs:
        response = DiceRunResponse(
            run_id=run_id, status="no_results", source="Dice",
            total_found=0, executed_at=now_iso, saved_to="", jobs=[],
        )
        await _mirror_run_to_db(response)
        return response.model_dump()

    response = DiceRunResponse(
        run_id=run_id, status="success", source="Dice",
        total_found=len(jobs), executed_at=now_iso, saved_to="", jobs=jobs,
    )
    # Results are persisted to the database only (see _mirror_run_to_db) —
    # no per-run JSON file is written anymore.
    log.info("dice_jobs_saved", count=len(jobs))

    await _mirror_run_to_db(response)
    return response.model_dump()


@router.get("/dice-results", status_code=status.HTTP_200_OK)
async def list_dice_results() -> Any:
    """List all saved Dice harvest run files, newest first."""
    mode = data_source_mode()
    runs, source = await resolve_read(
        mode,
        lambda: db_read(lambda db: HarvestRunService(db).list_runs(source="Dice")),
        lambda: _storage_svc.list_results(),
    )
    if source == "database":
        results = [run_to_result_summary(r) for r in runs] if runs else []
        return {"total_runs": len(results), "results": results}

    return {"total_runs": len(runs), "results": runs}


@router.get("/dice-results/{run_id}", status_code=status.HTTP_200_OK)
async def get_dice_result(run_id: str) -> Any:
    """Return the full JSON payload for a single saved Dice run."""
    mode = data_source_mode()
    run, source = await resolve_read(
        mode,
        lambda: db_read(lambda db: HarvestRunService(db).get_by_run_id(run_id, source="Dice")),
        lambda: _storage_svc.get_result(run_id),
    )
    if source == "database":
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No Dice result found for run_id '{run_id}'",
            )
        return _run_to_dice_payload(run)

    data = run
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No Dice result found for run_id '{run_id}'",
        )
    return data
