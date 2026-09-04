"""
LinkedIn Home Feed lead-harvest API route.

  POST /run-linkedin-feed-agent   Start a Home-Feed IT-hiring lead harvest (async).

Status is polled via the shared GET /harvest-status/{job_id} (the run carries a
job_id, so the existing endpoint finds it) — no dedicated status route needed.

Execution mirrors POST /run-harvest-agent: single-flight guard (the feed harvest
drives the SAME persistent Chrome profile as the job harvest, so the two must never
run at once), a background asyncio task, incremental + final persistence into the
shared harvest_runs / scraped_jobs / llm_calls tables (source="LinkedIn Feed"), and
JobTracker for live status.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import structlog
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse

from app.agents.linkedin_feed_agent import FeedAuthError, LinkedInFeedAgent
from app.config import get_settings
from app.core.exceptions import DailyJobLimitExceededError, JobAlreadyRunningError, LLMUnavailableError
from app.core.proactor import needs_proactor, run_in_proactor
from app.services import run_guard
from app.services.config_service import ConfigService
from app.services.harvest_run_service import (
    HarvestRunService,
    db_read,
    db_write,
    run_to_result_summary,
    scraped_job_view,
)
from app.services.job_tracker import JobTracker

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["LinkedIn Feed Harvest"])

_SOURCE = "LinkedIn Feed"
_SETUP_HINT = (
    "LinkedIn session is not authenticated. Run POST /linkedin-setup-session, log in "
    "at linkedin.com, close the browser, then re-run the Home-Feed harvest."
)


def _make_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_feed"


# ══════════════════════════════════════════════════════════════════════════════
# Background task
# ══════════════════════════════════════════════════════════════════════════════

async def _run_feed_background(job_id: str, run_id: str, run_pk: str | None) -> None:
    """Background entry point — always releases the single-flight guard."""
    try:
        await _run_feed_background_impl(job_id, run_id, run_pk)
    finally:
        run_guard.end()


async def _run_feed_background_impl(job_id: str, run_id: str, run_pk: str | None) -> None:
    log = logger.bind(job_id=job_id, run_id=run_id, source=_SOURCE)
    log.info("linkedin_feed_background_start")

    config   = ConfigService().load()
    headless = config.browser.resolved_headless
    slow_mo  = config.browser.slow_mo_ms

    JobTracker.update(job_id, progress=10, message="Opening LinkedIn Home Feed…")

    agent = LinkedInFeedAgent(get_settings())

    # Incremental persistence + live progress (mirrors run_harvest_agent). DB writes
    # from the proactor worker thread must be marshalled back to the main loop,
    # whose asyncpg pool is bound to it.
    main_loop = asyncio.get_event_loop()
    saved = {"n": 0}

    async def _do_write(coro) -> None:
        if needs_proactor():
            await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, main_loop))
        else:
            await coro

    async def _on_status(msg: str) -> None:
        JobTracker.update(job_id, message=msg)

    async def _on_batch(leads: list) -> None:
        if not run_pk or not leads:
            return
        dicts = [lead.to_scraped_job_dict() for lead in leads]
        await _do_write(db_write(lambda db: HarvestRunService(db).bulk_insert_scraped_jobs(run_pk, dicts)))
        saved["n"] += len(dicts)
        n = saved["n"]
        JobTracker.update(job_id, combined=n, progress=45, message=f"Harvesting… {n} leads saved so far")
        await _do_write(db_write(lambda db: HarvestRunService(db).update_run(run_pk, combined_count=n)))

    async def _do_harvest():
        leads = await agent.harvest(
            headless=headless, slow_mo=slow_mo, on_status=_on_status, on_batch=_on_batch,
        )
        return leads, agent.get_token_usage(), agent.get_llm_call_log()

    try:
        if needs_proactor():
            leads, token_usage, llm_calls = await run_in_proactor(_do_harvest)
        else:
            leads, token_usage, llm_calls = await _do_harvest()
    except FeedAuthError as exc:
        log.warning("linkedin_feed_action_required", error=str(exc))
        JobTracker.update(
            job_id, status="action_required", progress=100,
            message="LinkedIn session not authenticated", error=str(exc),
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        if run_pk:
            await db_write(lambda db: HarvestRunService(db).update_run(
                run_pk, status="action_required", progress=100,
                message=_SETUP_HINT, error=str(exc), completed_at=datetime.now(timezone.utc),
            ))
        return
    except LLMUnavailableError as exc:
        log.error("linkedin_feed_llm_unavailable", error=str(exc))
        JobTracker.update(
            job_id, status="failed", progress=100,
            message="LLM unavailable — harvest stopped", error=str(exc),
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        if run_pk:
            await db_write(lambda db: HarvestRunService(db).update_run(
                run_pk, status="failed", progress=100,
                message="LLM unavailable — harvest stopped", error=str(exc),
                completed_at=datetime.now(timezone.utc),
            ))
        return
    except Exception as exc:
        log.exception("linkedin_feed_background_error", error=str(exc))
        JobTracker.update(
            job_id, status="failed", progress=100,
            message=f"Feed harvest failed: {exc}", error=str(exc),
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        if run_pk:
            await db_write(lambda db: HarvestRunService(db).update_run(
                run_pk, status="failed", progress=100,
                message=f"Feed harvest failed: {exc}", error=str(exc),
                completed_at=datetime.now(timezone.utc),
            ))
        return

    # ── Reconcile final set + persist LLM audit ───────────────────────────────
    stopped = run_guard.is_stop_requested()
    final_dicts = [lead.to_scraped_job_dict() for lead in leads]
    if run_pk:
        await db_write(lambda db: HarvestRunService(db).replace_run_jobs(run_pk, final_dicts))
        await db_write(lambda db: HarvestRunService(db).bulk_insert_llm_calls(run_pk, llm_calls))

    status_str = "stopped" if stopped else ("success" if leads else "no_results")
    final_message = (
        f"Harvest stopped — {len(leads)} leads saved" if stopped
        else f"Harvest complete — {len(leads)} IT hiring leads"
    )
    log.info(
        "linkedin_feed_completed",
        leads=len(leads), status=status_str, token_usage=token_usage.get("total", {}),
    )
    JobTracker.update(
        job_id, status=status_str, progress=100, combined=len(leads),
        message=final_message, token_usage=token_usage,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )
    if run_pk:
        await db_write(lambda db: HarvestRunService(db).update_run(
            run_pk, status=status_str, progress=100, combined_count=len(leads),
            linkedin_count=len(leads), message=final_message, token_usage=token_usage,
            completed_at=datetime.now(timezone.utc),
        ))


# ══════════════════════════════════════════════════════════════════════════════
# POST /run-linkedin-feed-agent
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/run-linkedin-feed-agent", status_code=status.HTTP_202_ACCEPTED)
async def run_linkedin_feed_agent() -> Any:
    """
    Start a LinkedIn Home-Feed IT-hiring lead harvest in the background.

    Reads the whole authenticated Home Feed (NOT the Jobs board), classifies posts
    with the LLM, extracts job + recruiter info from genuine IT hiring posts, and
    persists them to scraped_jobs with source="LinkedIn Feed".

    Returns 202 with a `job_id`; poll **GET /harvest-status/{job_id}** for progress.
    """
    run_id = _make_run_id()

    # Global daily cap — reject before claiming the single-flight slot.
    budget = await run_guard.daily_budget_conflict()
    if budget is not None:
        raise DailyJobLimitExceededError(budget["used"], budget["limit"])

    # Single-flight: the feed harvest drives the same Chrome profile as the job
    # harvest, so only one may run at a time. DB backstop, then atomic in-process claim.
    db_conflict = await run_guard.db_conflict()
    if db_conflict is not None:
        raise JobAlreadyRunningError(db_conflict.get("job_id") or "", details=db_conflict)

    job_id = uuid4().hex
    conflict = run_guard.try_begin(job_id, run_id, _SOURCE)
    if conflict is not None:
        raise JobAlreadyRunningError(conflict.get("job_id") or "", details=conflict)

    try:
        JobTracker.create(job_id, run_id)
        run_pk = await db_write(lambda db: HarvestRunService(db).create_run(
            run_id=run_id,
            job_id=job_id,
            source=_SOURCE,
            sources=[_SOURCE],
            started_at=datetime.now(timezone.utc),
        ))
        logger.info("linkedin_feed_queued", job_id=job_id, run_id=run_id)
        asyncio.create_task(
            _run_feed_background(job_id, run_id, run_pk),
            name=f"linkedin-feed-{job_id}",
        )
    except Exception:
        # Background task never scheduled → its finally: run_guard.end() won't fire.
        run_guard.end()
        raise

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "job_id":  job_id,
            "run_id":  run_id,
            "source":  _SOURCE,
            "status":  "running",
            "message": "LinkedIn Home-Feed harvest started — poll GET /harvest-status/{job_id}",
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# GET /linkedin-feed-results  — saved Home-Feed lead runs (for the Source Runs UI)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/linkedin-feed-results", status_code=status.HTTP_200_OK)
async def list_linkedin_feed_results() -> Any:
    """List all saved LinkedIn Home-Feed lead runs, newest first. Reads the shared
    harvest_runs table filtered to source="LinkedIn Feed" (same shape the other
    per-source results endpoints return)."""
    runs = await db_read(lambda db: HarvestRunService(db).list_runs(source=_SOURCE)) or []
    return {"total_runs": len(runs), "results": [run_to_result_summary(r) for r in runs]}


@router.get("/linkedin-feed-results/{run_id}", status_code=status.HTTP_200_OK)
async def get_linkedin_feed_result(run_id: str) -> Any:
    """Return one Home-Feed run's extracted IT-hiring leads (from scraped_jobs)."""
    run = await db_read(lambda db: HarvestRunService(db).get_by_run_id(run_id, source=_SOURCE))
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No LinkedIn Feed result found for run_id '{run_id}'",
        )
    return {
        "run_id":      run.run_id,
        "executed_at": run.started_at.isoformat() if run.started_at else "",
        "status":      run.status,
        "source":      _SOURCE,
        "total_found": run.combined_count,
        "jobs":        [scraped_job_view(j) for j in run.jobs],
        "token_usage": run.token_usage or {},
    }
