"""
LinkedIn vendor-opportunity lead-harvest API route (runs separately from the job harvest).

  POST /run-linkedin-feed-agent?mode=both|feed|search
      Start a vendor-lead harvest (async): companies looking for staffing /
      recruitment vendors, read from the Home Feed ("feed"), LinkedIn post search
      for the configured vendor phrases ("search"), or both (default when
      LINKEDIN_FEED_SEARCH_ENABLED=true).

Status is polled via the shared GET /harvest-status/{job_id} (the run carries a
job_id, so the existing endpoint finds it) — no dedicated status route needed.

Execution mirrors POST /run-harvest-agent: single-flight guard (the feed harvest
drives the SAME persistent Chrome profile as the job harvest, so the two must never
run at once), a background asyncio task, incremental + final persistence into the
shared harvest_runs / scraped_jobs / llm_calls tables (source="LinkedIn Feed"), and
JobTracker for live status.

Tenant routing: the run row (and its llm_calls) belongs to the tenant that started
it, but each lead row is filed under its own market's client tenant — client_us /
client_in by phone, then location, then the extraction LLM's `market` field — and
falls back to the run's tenant when unclassifiable (app/services/feed_tenant_classifier.py).
The lead writes bind the session all-access so RLS (when active) allows that.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import structlog
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import JSONResponse

from app.agents.linkedin_feed_agent import MODES, FeedAuthError, LinkedInFeedAgent
from app.config import get_settings
from app.core.exceptions import DailyJobLimitExceededError, JobAlreadyRunningError, LLMUnavailableError
from app.core.proactor import needs_proactor, run_in_proactor
from app.core.tenant_context import bind_session_all_access, get_current_tenant_id
from app.services import run_guard
from app.services.config_service import ConfigService
from app.services.feed_tenant_classifier import classify_feed_lead_tenant
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
_MODE_LABELS = {"both": "Feed + Search", "feed": "Home Feed", "search": "Post Search"}
_SETUP_HINT = (
    "LinkedIn session is not authenticated. Run POST /linkedin-setup-session, log in "
    "at linkedin.com, close the browser, then re-run the LinkedIn vendor-lead harvest."
)


def _lead_dicts(leads: list, fallback_tenant: str) -> list[dict[str, Any]]:
    """ScrapedJobORM dicts for ``leads``, each stamped with its own ``tenant_id``:
    the lead's market tenant (client_us / client_in — phone, then location, then the
    extraction LLM's ``market``; app/services/feed_tenant_classifier.py), else
    ``fallback_tenant`` (the tenant that started the run)."""
    out: list[dict[str, Any]] = []
    for lead in leads:
        d = lead.to_scraped_job_dict()
        d["tenant_id"], signal = classify_feed_lead_tenant(
            fallback_tenant=fallback_tenant,
            phone=lead.recruiter_phone,
            location=lead.location,
            llm_market=getattr(lead, "market", None),
        )
        logger.debug("linkedin_feed_lead_tenant", url=d.get("job_url"),
                     tenant=d["tenant_id"], signal=signal)
        out.append(d)
    return out


def _make_run_id() -> str:
    """Build a Home-Feed run id: UTC timestamp + "_feed"."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_feed"


# ══════════════════════════════════════════════════════════════════════════════
# Background task
# ══════════════════════════════════════════════════════════════════════════════

async def _run_feed_background(job_id: str, run_id: str, run_pk: str | None, mode: str) -> None:
    """Background entry point — always releases the single-flight guard."""
    try:
        await _run_feed_background_impl(job_id, run_id, run_pk, mode)
    finally:
        run_guard.end()


async def _run_feed_background_impl(
    job_id: str, run_id: str, run_pk: str | None, mode: str,
) -> None:
    """Harvest LinkedIn vendor leads (Home Feed and/or post search, per `mode`) and record progress/results in JobTracker and the DB.

    Leads are inserted batch by batch as they are found; at the end the run's job set is replaced
    with the final list and the LLM call log stored. Ends the run as "success", "no_results" or
    "stopped" (stop requested), "action_required" when the LinkedIn session is not logged in, or
    "failed" (LLM unavailable / any other error). Never raises; DB writes are best-effort.
    """
    log = logger.bind(job_id=job_id, run_id=run_id, source=_SOURCE, mode=mode)
    log.info("linkedin_feed_background_start")

    config   = ConfigService().load()
    headless = config.browser.resolved_headless
    slow_mo  = config.browser.slow_mo_ms

    account  = str(config.sources.linkedin_account or "1")
    log.info("linkedin_feed_account", linkedin_account=account)
    JobTracker.update(
        job_id, progress=10,
        message=f"Opening LinkedIn Account {account} ({_MODE_LABELS.get(mode, mode)})…",
    )

    agent = LinkedInFeedAgent(get_settings())
    # The run (and its llm_calls) stay with the tenant that started it; each lead is
    # filed under its own market's tenant (_lead_dicts), falling back to this one.
    run_tenant = get_current_tenant_id()

    # Incremental persistence + live progress (mirrors run_harvest_agent). DB writes
    # from the proactor worker thread must be marshalled back to the main loop,
    # whose asyncpg pool is bound to it.
    main_loop = asyncio.get_event_loop()
    saved = {"n": 0}

    async def _do_write(coro) -> None:
        """Await a DB write coroutine on the main loop (whose asyncpg pool it needs) when running in
        the proactor worker thread; otherwise await it directly.
        """
        await _do_write_result(coro)

    async def _do_write_result(coro):
        """Like _do_write but returns the coroutine's result (db_write: None = failed)."""
        if needs_proactor():
            return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, main_loop))
        return await coro

    async def _on_status(msg: str) -> None:
        """Mirror an agent status message into the JobTracker entry."""
        JobTracker.update(job_id, message=msg)

    async def _on_batch(leads: list) -> None:
        """Persist one batch of leads and bump the live saved-count / progress message.

        No-op when there is no run row or the batch is empty.
        """
        if not run_pk or not leads:
            log.warning("linkedin_feed_batch_skipped", run_pk=run_pk, leads=len(leads or []))
            return
        dicts = _lead_dicts(leads, run_tenant)

        async def _insert(db) -> bool:
            await bind_session_all_access(db)  # leads may belong to another tenant (RLS)
            await HarvestRunService(db).bulk_insert_scraped_jobs(run_pk, dicts)
            return True

        # db_write swallows errors and returns None — log it so a lost batch is visible.
        ok = await _do_write_result(db_write(_insert))
        if not ok:
            log.error(
                "linkedin_feed_batch_insert_failed", run_pk=run_pk, batch=len(dicts),
                urls=[d.get("job_url") for d in dicts],
                hint="see the preceding harvest_db_mirror_failed / harvest_db_write_timeout",
            )
            return
        log.info("linkedin_feed_batch_inserted", run_pk=run_pk, batch=len(dicts),
                 total_saved=saved["n"] + len(dicts))
        saved["n"] += len(dicts)
        n = saved["n"]
        JobTracker.update(job_id, combined=n, progress=45, message=f"Harvesting… {n} leads saved so far")
        await _do_write(db_write(lambda db: HarvestRunService(db).update_run(run_pk, combined_count=n)))

    async def _do_harvest():
        """Run the feed agent; returns (leads, token usage, LLM call log)."""
        leads = await agent.harvest(
            headless=headless, slow_mo=slow_mo, on_status=_on_status, on_batch=_on_batch,
            mode=mode,
        )
        return leads, agent.get_token_usage(), agent.get_llm_call_log()

    try:
        if needs_proactor():
            leads, token_usage, llm_calls = await run_in_proactor(_do_harvest)
        else:
            leads, token_usage, llm_calls = await _do_harvest()
    except FeedAuthError as exc:
        if run_guard.is_stop_requested():
            # Stopped by the user while waiting for a LinkedIn login — not an auth failure.
            log.info("linkedin_feed_stopped_during_login_wait")
            JobTracker.update(
                job_id, status="stopped", progress=100,
                message="Harvest stopped while waiting for LinkedIn login",
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            if run_pk:
                await db_write(lambda db: HarvestRunService(db).update_run(
                    run_pk, status="stopped", progress=100,
                    message="Harvest stopped while waiting for LinkedIn login",
                    completed_at=datetime.now(timezone.utc),
                ))
            return
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
    interrupted = agent.collection_error
    final_dicts = _lead_dicts(leads, run_tenant)
    log.info(
        "linkedin_feed_reconcile_start", run_pk=run_pk, leads=len(final_dicts),
        incrementally_saved=saved["n"], llm_calls=len(llm_calls),
        collection_error=interrupted or None,
    )
    if run_pk:
        async def _replace(db) -> bool:
            # All-access: the run's incremental rows may sit in several tenants, and
            # the delete + reinsert must see/write all of them when RLS is active.
            await bind_session_all_access(db)
            return await HarvestRunService(db).replace_run_jobs(run_pk, final_dicts)

        replaced = await db_write(_replace)
        if replaced:
            log.info("linkedin_feed_reconcile_saved", run_pk=run_pk, leads=len(final_dicts))
        else:
            log.error(
                "linkedin_feed_reconcile_failed", run_pk=run_pk, leads=len(final_dicts),
                incrementally_saved=saved["n"],
                hint="final replace_run_jobs failed; only incremental batches (if any) are stored",
            )
        await db_write(lambda db: HarvestRunService(db).bulk_insert_llm_calls(run_pk, llm_calls))
    else:
        log.error("linkedin_feed_no_run_row", leads=len(final_dicts),
                  hint="harvest_runs row was not created — leads cannot be stored")

    status_str = "stopped" if stopped else ("success" if leads else "no_results")
    if stopped:
        final_message = f"Harvest stopped — {len(leads)} leads saved"
    elif interrupted:
        final_message = (
            f"Browser closed early — {len(leads)} vendor leads saved from the posts collected"
        )
    else:
        final_message = f"Harvest complete — {len(leads)} vendor leads"
    log.info(
        "linkedin_feed_completed",
        leads=len(leads), status=status_str, token_usage=token_usage.get("total", {}),
        collection_error=interrupted or None,
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

    # Apollo phones for saved leads with no email at companies above 500 employees
    # (app/services/feed_phone_service.py). Detached so its per-request pacing never
    # delays the run's completion or holds the harvest slot. Skipped on a user stop.
    if run_pk and leads and not stopped:
        from app.services.feed_phone_service import schedule_feed_lead_phones
        try:
            schedule_feed_lead_phones(run_pk)
        except Exception as exc:
            log.warning("linkedin_feed_phone_schedule_failed", error=str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# POST /run-linkedin-feed-agent
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/run-linkedin-feed-agent", status_code=status.HTTP_202_ACCEPTED)
async def run_linkedin_feed_agent(
    mode: str | None = Query(
        None,
        description='Where to read posts: "feed" (Home Feed), "search" (post search for the '
                    'configured vendor phrases) or "both". Default: "both" when '
                    'LINKEDIN_FEED_SEARCH_ENABLED=true, else "feed".',
    ),
) -> Any:
    """
    Start a LinkedIn vendor-opportunity lead harvest in the background.

    Finds posts where a company is looking for staffing / recruitment vendors
    (vendor registration / empanelment, C2C / C2H / contract requirements open to
    vendors) in the authenticated Home Feed and/or LinkedIn post search, classifies
    them with the LLM, extracts the opportunity + poster info, and persists them to
    scraped_jobs with source="LinkedIn Feed". Runs separately from the job harvest
    but shares its LinkedIn session, so the two never run at once.

    Returns 202 with a `job_id`; poll **GET /harvest-status/{job_id}** for progress.
    """
    settings = get_settings()
    if mode is None:
        mode = "both" if settings.linkedin_feed_search_enabled else "feed"
    if mode not in MODES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"mode must be one of {list(MODES)}",
        )
    run_id = _make_run_id()
    # The run uses the Rule Engine's active LinkedIn account (sources.linkedin_account);
    # recorded on the run so the history shows which account harvested it.
    account = str(ConfigService().load().sources.linkedin_account or "1")

    # Global daily cap — reject before claiming the single-flight slot.
    budget = await run_guard.daily_budget_conflict()
    if budget is not None:
        raise DailyJobLimitExceededError(budget["used"], budget["limit"])

    # Single-flight: the feed harvest drives the same Chrome profile as the job
    # harvest, so only one may run at a time. DB backstop, then atomic in-process claim.
    # conflict_details hides another tenant's job_id/run_id (it only learns "busy").
    db_conflict = await run_guard.db_conflict()
    if db_conflict is not None:
        shown = run_guard.conflict_details(db_conflict)
        raise JobAlreadyRunningError(shown.get("job_id") or "", details=shown)

    job_id = uuid4().hex
    conflict = run_guard.try_begin(job_id, run_id, _SOURCE)
    if conflict is not None:
        shown = run_guard.conflict_details(conflict)
        raise JobAlreadyRunningError(shown.get("job_id") or "", details=shown)

    try:
        JobTracker.create(job_id, run_id)
        run_pk = await db_write(lambda db: HarvestRunService(db).create_run(
            run_id=run_id,
            job_id=job_id,
            source=_SOURCE,
            sources=[_SOURCE],
            filters_snapshot={
                "mode": mode,
                "linkedin_account": account,
                "search_queries": settings.linkedin_feed_search_query_list if mode != "feed" else [],
                "search_date_posted": settings.linkedin_feed_search_date_posted if mode != "feed" else "",
            },
            started_at=datetime.now(timezone.utc),
        ))
        logger.info("linkedin_feed_queued", job_id=job_id, run_id=run_id, mode=mode,
                    linkedin_account=account)
        asyncio.create_task(
            _run_feed_background(job_id, run_id, run_pk, mode),
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
            "mode":    mode,
            "linkedin_account": account,
            "status":  "running",
            "message": f"LinkedIn vendor-lead harvest started ({_MODE_LABELS[mode]}, "
                       f"LinkedIn Account {account}) — poll GET /harvest-status/{{job_id}}",
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# GET /linkedin-feed-results  — saved Home-Feed lead runs (for the Source Runs UI)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/linkedin-feed-results", status_code=status.HTTP_200_OK)
async def list_linkedin_feed_results() -> Any:
    """List all saved LinkedIn vendor-lead runs, newest first. Reads the shared
    harvest_runs table filtered to source="LinkedIn Feed" (same shape the other
    per-source results endpoints return, plus `mode` and `keyword` = the run's mode
    label so the Source Runs table shows how each run was made)."""
    runs = await db_read(lambda db: HarvestRunService(db).list_runs(source=_SOURCE)) or []
    results = []
    for r in runs:
        row = run_to_result_summary(r)
        mode = (r.filters_snapshot or {}).get("mode") or "feed"  # pre-mode runs were feed-only
        row["mode"] = mode
        row["keyword"] = _MODE_LABELS.get(mode, mode)
        results.append(row)
    return {"total_runs": len(results), "results": results}


@router.get("/linkedin-feed-results/{run_id}", status_code=status.HTTP_200_OK)
async def get_linkedin_feed_result(run_id: str) -> Any:
    """Return one run's extracted vendor leads (from scraped_jobs)."""
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
        "mode":        (run.filters_snapshot or {}).get("mode") or "feed",
        "total_found": run.combined_count,
        "jobs":        [scraped_job_view(j) for j in run.jobs],
        "token_usage": run.token_usage or {},
    }
