"""
POST /run-harvest-agent — unified harvest trigger (async / background).
GET  /harvest-status/{job_id} — live status polling.

Design contract
───────────────
• POST accepts ONLY {} or {"config_id": "active"}.  No search criteria.
  All harvesting parameters come from harvest_config.json.
• The endpoint returns HTTP 202 immediately with a job_id.
  The harvest runs in the background and can be polled via GET /harvest-status/{job_id}.

Execution flow (background task)
────────────────────────────────
1. Load harvest_config.json  (config_service)
2. Determine enabled sources
3. Run sources in priority order: Naukri → LinkedIn → Dice  (via OrchestratorAgent)
4. Apply business filters (domain / hiring entity / GCC / salary / work mode)
5. Run company verification if enabled
6. Save per-source result files to data/results/<source>/
7. Update data/results/run_history/run_history.json
8. Mark job complete in JobTracker

Response (202)
──────────────
{
    "job_id":  "a1b2c3d4...",
    "status":  "running",
    "message": "Harvest started in background"
}

Poll GET /harvest-status/{job_id} for progress and final results.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import structlog
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.agents.orchestrator_agent import OrchestratorAgent, OrchestratorResult
from app.config import get_settings
from app.core.exceptions import DailyJobLimitExceededError, JobAlreadyRunningError, LLMUnavailableError
from app.core.proactor import needs_proactor, run_in_proactor
from app.models.harvest_run import HarvestRunORM
from app.services.config_service import ConfigService
from app.services.email_service import EmailSender
from app.services.harvest_notification_service import send_harvest_report
from app.services.harvest_run_service import (
    HarvestRunService,
    data_source_mode,
    db_read,
    db_write,
    resolve_read,
)
from app.services import run_guard
from app.services.job_tracker import JobTracker
from app.services.report_service import compute_harvest_insights, merged_job_dicts
from app.services.run_history_service import RunHistoryService

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Harvest Agent"])

_config_svc  = ConfigService()
_history_svc = RunHistoryService()


# ── Request model ──────────────────────────────────────────────────────────────

class HarvestAgentRequest(BaseModel):
    """
    Execution trigger payload.

    Only config_id is accepted — no search criteria.
    All harvesting parameters are loaded from harvest_config.json.
    """
    config_id: str = "active"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _err(msg: str, reason: str = "") -> JSONResponse:
    body: dict[str, Any] = {"status": "failed", "message": msg}
    if reason:
        body["reason"] = reason
    return JSONResponse(status_code=200, content=body)


def _filters_snapshot(cfg) -> dict:
    f = cfg.filters
    return {
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
        "verification_enabled":     f.verification.enabled,
    }


def _run_to_job_status_dict(run: HarvestRunORM) -> dict[str, Any]:
    """Maps a HarvestRunORM row onto the exact shape JobStatus.to_dict()
    already returns, so GET /harvest-status/{job_id} is unchanged for callers."""
    return {
        "job_id":       run.job_id or "",
        "run_id":       run.run_id,
        "status":       run.status,
        "progress":     run.progress,
        "message":      run.message or "",
        "linkedin":     run.linkedin_count,
        "naukri":       run.naukri_count,
        "dice":         run.dice_count,
        "combined":     run.combined_count,
        "started_at":   run.started_at.isoformat() if run.started_at else "",
        "completed_at": run.completed_at.isoformat() if run.completed_at else "",
        "excel_path":   run.excel_path or "",
        "json_path":    run.json_path or "",
        "error":        run.error or "",
        "token_usage":  run.token_usage or {},
    }


def _run_to_history_entry(run: HarvestRunORM) -> dict[str, Any]:
    """Maps a HarvestRunORM row onto the exact shape RunHistoryService.make_entry()
    already returns, so GET /run-history[/{run_id}] is unchanged for callers."""
    return {
        "run_id":         run.run_id,
        "sources":        run.sources or [],
        "started_at":     run.started_at.isoformat() if run.started_at else "",
        "completed_at":   run.completed_at.isoformat() if run.completed_at else "",
        "status":         run.status,
        "jobs_found":     run.combined_count,
        "verified_jobs":  run.verified_jobs,
        "direct_clients": run.direct_clients,
        "gcc":            run.gcc,
        "staffing_firms": run.staffing_firms,
        "ambiguous":      run.ambiguous,
        "error":          run.error,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Background harvest task
# ══════════════════════════════════════════════════════════════════════════════

async def _run_harvest_background(
    job_id:   str,
    run_id:   str,
    config:   Any,
    now_iso:  str,
    enabled:  list[str],
    run_pk:   str | None = None,
    wait_for_login: bool = True,
) -> None:
    """Background entry point — releases the single-flight guard no matter how
    the run ends (success, LLM outage, or unexpected failure)."""
    try:
        await _run_harvest_background_impl(job_id, run_id, config, now_iso, enabled, run_pk, wait_for_login)
    finally:
        run_guard.end()


async def _run_harvest_background_impl(
    job_id:   str,
    run_id:   str,
    config:   Any,
    now_iso:  str,
    enabled:  list[str],
    run_pk:   str | None = None,
    wait_for_login: bool = True,
) -> None:
    """Runs the full harvest in a background asyncio task, updating JobTracker."""
    log = logger.bind(job_id=job_id, run_id=run_id, sources=enabled)
    log.info("harvest_background_start")

    JobTracker.update(job_id, progress=10, message="Starting orchestrator")

    orch = OrchestratorAgent(config)

    async def _on_status(msg: str) -> None:
        # Surfaces live progress (e.g. "waiting for LinkedIn login…") to
        # GET /harvest-status/{job_id} while the run is still in flight.
        JobTracker.update(job_id, message=msg)
        log.info("harvest_status_update", message=msg)

    # ── Incremental persistence + live progress ───────────────────────────────
    # As each LinkedIn page (and each other source) is scraped, persist those
    # jobs to the DB immediately so a mid-scrape crash/kill keeps them — and bump
    # a live "jobs saved" count the frontend polls via GET /harvest-status. These
    # rows are RAW (pre-classification/dedup); the end-of-run reconcile below
    # replaces them atomically with the final result.all_jobs.
    main_loop = asyncio.get_event_loop()
    saved_counter = {"n": 0}

    async def _do_write(coro) -> None:
        # On Windows --reload, run_all runs on a proactor worker thread; the
        # asyncpg pool is bound to the main loop, so marshal DB writes back to it.
        # On Linux/EC2 (needs_proactor False) run_all is on the main loop already.
        if needs_proactor():
            await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, main_loop))
        else:
            await coro

    async def _persist(unified: list) -> None:
        if not run_pk or not unified:
            return
        try:
            dicts = [j.to_dict() for j in unified]
            await _do_write(db_write(lambda db: HarvestRunService(db).bulk_insert_scraped_jobs(run_pk, dicts)))
            saved_counter["n"] += len(dicts)
            n = saved_counter["n"]
            msg = f"Harvesting… {n} jobs saved so far"
            JobTracker.update(job_id, combined=n, message=msg)
            await _do_write(db_write(lambda db: HarvestRunService(db).update_run(run_pk, combined_count=n, message=msg)))
            log.info("harvest_incremental_persist", saved=n, batch=len(dicts))
        except Exception as exc:
            # Best-effort: a persist hiccup must never abort the scrape — the
            # end-of-run reconcile re-writes the canonical set regardless.
            log.warning("harvest_incremental_persist_failed", error=str(exc))

    try:
        # wait_for_login: a manually-triggered run (POST /run-harvest-agent)
        # passes True — if LinkedIn isn't authenticated, pause and wait for a
        # human to log in via "Watch Live Browser" instead of failing
        # immediately. The scheduled/unattended path passes False: no one is
        # present to complete a login, so fail fast instead of hanging.
        if needs_proactor():
            log.debug("using_proactor_thread")
            result: OrchestratorResult = await run_in_proactor(
                lambda: orch.run_all(wait_for_login=wait_for_login, on_status=_on_status, on_persist=_persist)
            )
        else:
            result = await orch.run_all(wait_for_login=wait_for_login, on_status=_on_status, on_persist=_persist)
    except LLMUnavailableError as exc:
        # The extraction LLM went down mid-run. Per product requirement: stop
        # the whole scrape, surface the provider error verbatim (the local-LLM
        # message names LOCAL_LLM_MODEL + "contact the admin team"), but KEEP
        # whatever jobs were scraped before the outage.
        err_msg = exc.message
        log.error("harvest_background_llm_unavailable", error=err_msg)
        partial: OrchestratorResult | None = getattr(exc, "partial_result", None)

        if run_pk and partial is not None:
            if partial.all_jobs:
                # Replace any provisional incrementally-persisted rows with the
                # partial set (delete + insert atomically).
                scraped_dicts = [j.to_dict() for j in partial.all_jobs]
                await db_write(lambda db: HarvestRunService(db).replace_run_jobs(run_pk, scraped_dicts))
            if partial.llm_calls:
                await db_write(lambda db: HarvestRunService(db).bulk_insert_llm_calls(run_pk, partial.llm_calls))

        partial_count = partial.total_jobs if partial is not None else 0
        _history_svc.append(
            RunHistoryService.make_entry(
                run_id       = run_id,
                sources      = enabled,
                started_at   = datetime.now(timezone.utc),
                completed_at = datetime.now(timezone.utc),
                status       = "failed",
                jobs_found   = partial_count,
                error        = err_msg,
            )
        )
        JobTracker.update(
            job_id,
            status       = "failed",
            progress     = 100,
            message      = "LLM unavailable — scraping stopped",
            error        = err_msg,
            combined     = partial_count,
            completed_at = datetime.now(timezone.utc).isoformat(),
        )
        if run_pk:
            await db_write(lambda db: HarvestRunService(db).update_run(
                run_pk,
                status         = "failed",
                progress       = 100,
                message        = "LLM unavailable — scraping stopped",
                error          = err_msg,
                combined_count = partial_count,
                completed_at   = datetime.now(timezone.utc),
            ))

        await send_harvest_report(
            EmailSender(get_settings()),
            config.notifications,
            run_id     = run_id,
            status     = "failed",
            total_jobs = partial_count,
            sources    = enabled,
            error      = err_msg,
        )
        return
    except Exception as exc:
        log.exception("harvest_background_error", error=str(exc))
        _history_svc.append(
            RunHistoryService.make_entry(
                run_id       = run_id,
                sources      = enabled,
                started_at   = datetime.now(timezone.utc),
                completed_at = datetime.now(timezone.utc),
                status       = "failed",
                jobs_found   = 0,
                error        = str(exc),
            )
        )
        JobTracker.update(
            job_id,
            status       = "failed",
            progress     = 100,
            message      = f"Harvest failed: {exc}",
            error        = str(exc),
            completed_at = datetime.now(timezone.utc).isoformat(),
        )
        if run_pk:
            await db_write(lambda db: HarvestRunService(db).update_run(
                run_pk,
                status       = "failed",
                progress     = 100,
                message      = f"Harvest failed: {exc}",
                error        = str(exc),
                completed_at = datetime.now(timezone.utc),
            ))

        # ── Alert configured recipients that the run failed (best-effort) ────────
        await send_harvest_report(
            EmailSender(get_settings()),
            config.notifications,
            run_id     = run_id,
            status     = "failed",
            total_jobs = 0,
            sources    = enabled,
            error      = str(exc),
        )
        return

    JobTracker.update(
        job_id,
        progress = 70,
        message  = "Saving results",
        linkedin = len(result.jobs_by_source.get("LinkedIn", [])),
        naukri   = len(result.jobs_by_source.get("Naukri",   [])),
        dice     = len(result.jobs_by_source.get("Dice",     [])),
        combined = result.total_jobs,
    )
    if run_pk:
        await db_write(lambda db: HarvestRunService(db).update_run(
            run_pk,
            progress       = 70,
            message        = "Saving results",
            linkedin_count = len(result.jobs_by_source.get("LinkedIn", [])),
            naukri_count   = len(result.jobs_by_source.get("Naukri",   [])),
            dice_count     = len(result.jobs_by_source.get("Dice",     [])),
            combined_count = result.total_jobs,
        ))

    # ── Persist the final deduped/filtered/verified job list + LLM call log ──
    # The database is now the only store — per-source/combined JSON and Excel
    # result files are no longer written; reports are generated from these
    # rows on demand (GET /download/{json,excel}, the report email below).
    # Reconcile: replace_run_jobs atomically deletes the provisional rows written
    # incrementally during the scrape and inserts the final classified/deduped
    # set, so the end state is exactly result.all_jobs with no duplicates.
    if run_pk:
        scraped_dicts = [j.to_dict() for j in result.all_jobs]
        await db_write(lambda db: HarvestRunService(db).replace_run_jobs(run_pk, scraped_dicts))
        await db_write(lambda db: HarvestRunService(db).bulk_insert_llm_calls(run_pk, result.llm_calls))

    # A cooperative user-stop lets run_all() return normally with the jobs
    # gathered so far (the scrape loops break and return early — see
    # linkedin_agent). Persist them exactly like a normal completion, but mark
    # the run "stopped", flag its report as owed (report_pending), and skip the
    # email — the next successful run merges these jobs into its own report.
    stopped = run_guard.is_stop_requested()

    # ── Update run history ────────────────────────────────────────────────────
    status_str = "stopped" if stopped else ("success" if result.total_jobs > 0 else "no_results")
    history_entry = RunHistoryService.make_entry(
        run_id          = run_id,
        sources         = result.sources_executed,
        started_at      = result.started_at,
        completed_at    = result.completed_at,
        status          = status_str,
        jobs_found      = result.total_jobs,
        verified_jobs   = result.verified_jobs,
        direct_clients  = result.direct_clients,
        gcc             = result.gcc,
        staffing_firms  = result.staffing_firms,
        ambiguous       = result.ambiguous,
    )
    try:
        _history_svc.append(history_entry)
    except Exception as exc:
        log.warning("history_save_failed", error=str(exc))

    elapsed_seconds = (result.completed_at - result.started_at).total_seconds()
    log.info(
        "harvest_completed",
        run_id         = run_id,
        total          = result.total_jobs,
        verified       = result.verified_jobs,
        direct_clients = result.direct_clients,
        gcc            = result.gcc,
        staffing_firms = result.staffing_firms,
        ambiguous      = result.ambiguous,
        sources        = result.sources_executed,
        runtime_min    = round(elapsed_seconds / 60, 1),
        combined_path  = result.combined_path,
        token_usage    = result.token_usage.get("total", {}),
    )

    final_message = (
        f"Harvest stopped — {result.total_jobs} jobs saved"
        if stopped
        else f"Harvest complete — {result.total_jobs} jobs found"
    )
    JobTracker.update(
        job_id,
        status       = status_str,
        progress     = 100,
        message      = final_message,
        completed_at = result.completed_at.isoformat(),
        excel_path   = result.excel_path   or "",
        json_path    = result.combined_path or "",
        token_usage  = result.token_usage,
    )
    if run_pk:
        await db_write(lambda db: HarvestRunService(db).update_run(
            run_pk,
            status          = status_str,
            progress        = 100,
            message         = final_message,
            completed_at    = result.completed_at,
            excel_path      = result.excel_path or None,
            json_path       = result.combined_path or None,
            token_usage     = result.token_usage,
            verified_jobs   = result.verified_jobs,
            direct_clients  = result.direct_clients,
            gcc             = result.gcc,
            staffing_firms  = result.staffing_firms,
            ambiguous       = result.ambiguous,
            sources         = result.sources_executed,
            # A stopped run with jobs owes a deferred report; a stopped run with
            # zero jobs has nothing to defer.
            report_pending  = stopped and result.total_jobs > 0,
        ))

    # A user-stopped run defers its report: jobs are saved and flagged
    # report_pending above, and the email goes out with the next successful run.
    if stopped:
        log.info("harvest_stopped_report_deferred", run_id=run_id, total=result.total_jobs)
        return

    # ── Email the report to configured recipients (best-effort) ──────────────
    # Attachments are built in memory from this run's DB rows so the
    # recruiter-merged email/phone (scraped_job_view) is what recipients see;
    # falls back to the in-memory scraped list if the DB mirror failed.
    report_dicts: list[dict] = []
    # ORM rows kept alongside the dicts purely to compute the email insights
    # (source split + local-LLM/Apollo contact provenance) — provenance lives on
    # the linked recruiter, which the dicts/UI view don't carry.
    insight_rows: list = []
    if run_pk:
        run_rows = await db_read(lambda db: HarvestRunService(db).list_jobs_for_run(run_pk))
        if run_rows:
            report_dicts = merged_job_dicts(run_rows)
            insight_rows = list(run_rows)
    if not report_dicts:
        report_dicts = [j.to_dict() for j in result.all_jobs]

    # ── Flush any deferred reports owed by previously user-stopped runs ───────
    # Their jobs are merged into THIS report so a single email carries both.
    # Only when notifications are enabled — otherwise leave them pending rather
    # than silently clearing a report that never actually went out.
    total_for_email = result.total_jobs
    if config.notifications.enabled:
        pending_runs = await db_read(lambda db: HarvestRunService(db).list_pending_report_runs()) or []
        merged_pending = 0
        for pend in pending_runs:
            if pend.id == run_pk:
                continue
            pend_rows = await db_read(lambda db: HarvestRunService(db).list_jobs_for_run(pend.id))
            if pend_rows:
                report_dicts = report_dicts + merged_job_dicts(pend_rows)
                insight_rows = insight_rows + list(pend_rows)
                merged_pending += len(pend_rows)
            await db_write(lambda db: HarvestRunService(db).clear_report_pending(pend.id))
        if merged_pending:
            total_for_email = len(report_dicts)
            log.info("deferred_reports_merged", runs=len(pending_runs), jobs=merged_pending)

    # Report-email-only insights (never surfaced in the UI) built from the DB
    # rows: with/without LinkedIn, records carrying email/phone, and how many
    # contacts came from the local LLM/scrape vs paid Apollo enrichment.
    insights = compute_harvest_insights(insight_rows) if insight_rows else None

    await send_harvest_report(
        EmailSender(get_settings()),
        config.notifications,
        run_id      = run_id,
        status      = status_str,
        total_jobs  = total_for_email,
        sources     = result.sources_executed,
        job_dicts   = report_dicts,
        insights    = insights,
    )


# ══════════════════════════════════════════════════════════════════════════════
# POST /run-harvest-agent
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/run-harvest-agent", status_code=status.HTTP_202_ACCEPTED)
async def run_harvest_agent(body: HarvestAgentRequest = HarvestAgentRequest()) -> Any:
    """
    Trigger a full harvest run from the current harvest_config.json settings.

    Returns immediately (HTTP 202) with a `job_id`.
    Poll **GET /harvest-status/{job_id}** for live progress and final results.

    All search filters (keyword, location, job_type, work_mode, domain,
    hiring_entity, GCC mode, salary, verification) are read from the saved
    config — this endpoint accepts **no** filter parameters.

    Execution order: Naukri (1) → LinkedIn (2) → Dice (3)
    """
    config  = _config_svc.load()
    run_id  = _make_run_id()
    now_iso = datetime.now(timezone.utc).isoformat()

    logger.info(
        "config_loaded",
        keyword              = config.filters.keyword,
        location             = config.filters.location,
        job_type             = config.filters.job_type,
        work_mode            = config.filters.work_mode,
        search_window_hours  = config.filters.search_window_hours,
        max_jobs             = config.filters.max_jobs,
        domain               = config.filters.domain,
        hiring_entity        = config.filters.hiring_entity,
    )

    enabled = [
        src for src in ["naukri", "linkedin", "dice"]
        if getattr(config.sources, src, False)
    ]
    if not enabled:
        return _err(
            "No sources enabled",
            "Enable at least one source (linkedin, naukri, or dice) in harvest_config.json",
        )

    # ── Global daily cap: reject before claiming the single-flight slot ───────
    budget = await run_guard.daily_budget_conflict()
    if budget is not None:
        raise DailyJobLimitExceededError(budget["used"], budget["limit"])

    # ── Single-flight: reject if a harvest is already running ─────────────────
    # DB backstop first (catches a run left 'running' by a previous process),
    # then an atomic in-process claim. try_begin() has no await between its
    # check and set, so two near-simultaneous starts (a double-click, two tabs,
    # two users) can't both pass it — the first wins, the rest get 409. The old
    # check_conflict()+begin() pair awaited the DB read between check and set,
    # leaving that race open (both could start and drive the same Chrome
    # profile at once — exactly what this guard exists to prevent).
    db_conflict = await run_guard.db_conflict()
    if db_conflict is not None:
        raise JobAlreadyRunningError(db_conflict.get("job_id") or "", details=db_conflict)

    job_id = uuid4().hex
    conflict = run_guard.try_begin(job_id, run_id, None)
    if conflict is not None:
        raise JobAlreadyRunningError(conflict.get("job_id") or "", details=conflict)

    # The slot is now claimed. Anything that fails before the background task is
    # scheduled must release it, or the lock would stick until the next restart.
    try:
        JobTracker.create(job_id, run_id)

        run_pk = await db_write(lambda db: HarvestRunService(db).create_run(
            run_id           = run_id,
            job_id           = job_id,
            source           = None,
            sources          = enabled,
            filters_snapshot = _filters_snapshot(config),
            started_at       = datetime.now(timezone.utc),
        ))

        logger.info(
            "harvest_agent_queued",
            job_id    = job_id,
            run_id    = run_id,
            sources   = enabled,
            keyword   = config.filters.keyword,
            config_id = body.config_id,
        )

        asyncio.create_task(
            _run_harvest_background(job_id, run_id, config, now_iso, enabled, run_pk),
            name = f"harvest-{job_id}",
        )
    except Exception:
        # Background task never started → its finally: run_guard.end() will
        # never fire, so release the slot here. end() is idempotent.
        run_guard.end()
        raise

    return JSONResponse(
        status_code = status.HTTP_202_ACCEPTED,
        content = {
            "job_id":  job_id,
            "run_id":  run_id,
            "status":  "running",
            "message": "Harvest started in background — poll GET /harvest-status/{job_id}",
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# POST /run-harvest-agent/stop
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/run-harvest-agent/stop", status_code=status.HTTP_200_OK)
async def stop_harvest_agent() -> Any:
    """Request a cooperative stop of the in-flight harvest.

    Sets a stop flag the running scrape polls at its loop checkpoints; the run
    then returns the jobs gathered so far, which are persisted to the DB and the
    run is marked 'stopped'. No report email is sent now — those jobs are merged
    into the next successful run's report. This never hard-kills the browser, so
    the persistent Chrome profile closes cleanly (no lock corruption).

    Safe to call when nothing is running (returns status 'idle').
    """
    active = run_guard.active_run()
    if active is None:
        return {"status": "idle", "message": "No harvest is currently running."}

    run_guard.request_stop()
    job_id = active.get("job_id")
    if job_id:
        JobTracker.update(job_id, message="Stop requested — finishing current step and saving jobs…")
    logger.info("harvest_stop_requested", **active)
    return {
        "status":  "stopping",
        "job_id":  job_id,
        "run_id":  active.get("run_id"),
        "message": "Stop requested — the run will halt shortly and save its jobs.",
    }


# ══════════════════════════════════════════════════════════════════════════════
# GET /active-run
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/active-run", status_code=status.HTTP_200_OK)
async def get_active_run() -> Any:
    """Whether a harvest is currently running (any source). The frontend calls
    this on load and after a run ends to freeze/unfreeze its Run controls, so a
    second browser tab reflects a run started elsewhere. Cheap: no polling loop
    needed, single indexed lookup."""
    conflict = await run_guard.check_conflict()
    return {
        "active": conflict is not None,
        "job_id": (conflict or {}).get("job_id"),
        "run_id": (conflict or {}).get("run_id"),
        "source": (conflict or {}).get("source"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# GET /harvest-status/{job_id}
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/harvest-status/{job_id}", status_code=status.HTTP_200_OK)
async def get_harvest_status(job_id: str) -> Any:
    """
    Poll the status of a background harvest job.

    Returns progress (0–100), per-source counts, and output file paths
    once the harvest completes.

    Possible `status` values:
    - `running`    — harvest is in progress
    - `success`    — harvest completed with results
    - `no_results` — harvest completed but found no matching jobs
    - `failed`     — harvest error (check `error` field)
    - `stopped`    — harvest was stopped by the user (partial jobs saved; report deferred)
    """
    mode = data_source_mode()
    run, source = await resolve_read(
        mode,
        lambda: db_read(lambda db: HarvestRunService(db).get_by_job_id(job_id)),
        lambda: JobTracker.get(job_id),
    )
    # Global daily cap (MAX_JOBS_PER_DAY, 0 = unlimited) so the frontend progress
    # cards can show Max / Saved / Remaining without duplicating the env value.
    max_per_day = get_settings().max_jobs_per_day
    # Live DB count of rows persisted today (across all of today's runs) — backs
    # the "Jobs Saved" card so it reflects real scraped_jobs rows, climbing as
    # each batch inserts. Best-effort: db_read returns None if the DB is
    # unavailable → fall back to the run's own combined_count below.
    saved_today = await db_read(lambda db: HarvestRunService(db).jobs_saved_today())

    if source == "database":
        if run is None:
            return JSONResponse(
                status_code = 404,
                content     = {"detail": f"No harvest job found with id '{job_id}'"},
            )
        payload = _run_to_job_status_dict(run)
        payload["max_jobs_per_day"] = max_per_day
        payload["jobs_saved_today"] = saved_today if saved_today is not None else payload.get("combined", 0)
        return payload

    # source == "json": run is whatever JobTracker.get() returned (predates
    # the DB mirror, or DATA_SOURCE=json / DB temporarily unavailable in "auto").
    if run is None:
        return JSONResponse(
            status_code = 404,
            content     = {"detail": f"No harvest job found with id '{job_id}'"},
        )
    payload = run.to_dict()
    payload["max_jobs_per_day"] = max_per_day
    payload["jobs_saved_today"] = saved_today if saved_today is not None else payload.get("combined", 0)
    return payload


# ══════════════════════════════════════════════════════════════════════════════
# GET /run-history
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/run-history", status_code=status.HTTP_200_OK)
async def get_run_history() -> Any:
    """Return all harvest run history entries, newest first."""
    mode = data_source_mode()
    runs, source = await resolve_read(
        mode,
        lambda: db_read(lambda db: HarvestRunService(db).list_runs(source=None)),
        lambda: _history_svc.list_all(),
    )
    if source == "database":
        entries = [_run_to_history_entry(r) for r in runs] if runs else []
        return {"total_runs": len(entries), "runs": entries}

    return {"total_runs": len(runs), "runs": runs}


@router.get("/run-history/{run_id}", status_code=status.HTTP_200_OK)
async def get_run_history_entry(run_id: str) -> Any:
    """Return a single run history entry by run_id."""
    mode = data_source_mode()
    run, source = await resolve_read(
        mode,
        lambda: db_read(lambda db: HarvestRunService(db).get_by_run_id(run_id, source=None)),
        lambda: _history_svc.get(run_id),
    )
    if source == "database":
        if run is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"No result found for run_id '{run_id}'"},
            )
        return _run_to_history_entry(run)

    entry = run
    if entry is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"No result found for run_id '{run_id}'"},
        )
    return entry
