"""Process-wide single-flight guard for harvest runs.

Only one harvest may run at a time: the orchestrator and every single-source
agent drive Playwright against the *same* persistent Chrome profile, which
cannot be opened twice — a second concurrent run corrupts the profile lock and
fails both runs. Today the only protection is a per-tab frontend flag and an
in-process browser lock that merely makes the second run *queue*; this guard
rejects the second start outright.

Two layers:
  • an in-process marker (`_active`) — the authoritative guard for the single
    uvicorn worker this app runs as; checked/set synchronously so concurrent
    starts can't interleave past it.
  • a DB backstop (`HarvestRunService.get_active_run`) — catches a run left
    `running` by a previous process (e.g. after a restart) and would cover a
    hypothetical multi-worker deployment.

Callers:
  - POST /run-harvest-agent          (multi-source, background task)
  - POST /run-{linkedin,naukri,dice}-agent  (single-source, synchronous)
  - the scheduler's unattended run
"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager

import structlog

from app.config import get_settings
from app.core.exceptions import DailyJobLimitExceededError, JobAlreadyRunningError
from app.services.harvest_run_service import HarvestRunService, db_read

logger = structlog.get_logger(__name__)

# {"job_id", "run_id", "source"} while a run is in flight in THIS process; None otherwise.
_active: dict | None = None

# Cooperative-stop signal for the in-flight run. A user-triggered stop sets this;
# the scrape loops (linkedin_agent) poll it and break, returning the jobs
# collected so far so the run can persist a partial harvest and close the browser
# cleanly. A threading.Event (not asyncio) is used deliberately: on Windows the
# scrape runs on a separate proactor thread (app.core.proactor), and only a
# thread-safe primitive is reliably visible from there. Single-flight guarantees
# at most one run, so one process-global event is sufficient.
_stop_event = threading.Event()


def request_stop() -> None:
    """Ask the in-flight run to stop cooperatively at the next loop checkpoint."""
    _stop_event.set()
    logger.info("run_guard_stop_requested", **(dict(_active) if _active else {}))


def clear_stop() -> None:
    """Reset the stop signal (called when a run begins/ends)."""
    _stop_event.clear()


def is_stop_requested() -> bool:
    """True if a cooperative stop has been requested for the in-flight run."""
    return _stop_event.is_set()


def active_run() -> dict | None:
    """The run this process currently considers in-flight, or None."""
    return dict(_active) if _active else None


async def check_conflict() -> dict | None:
    """Return conflict info if a harvest is already running (in-process marker
    first, then the DB backstop), else None.

    Read-only — safe for status endpoints (GET /active-run). Do NOT use this to
    gate a start: it awaits (the DB read) between reading `_active` and the
    caller's later `begin()`, and two concurrent starts can both pass it before
    either claims the slot. Use `try_begin()` for the start path instead."""
    if _active is not None:
        return dict(_active)
    return await db_conflict()


async def db_conflict() -> dict | None:
    """DB backstop only: info for a run left 'running' by a previous process
    (e.g. an ungraceful restart), else None. Does NOT consult the in-process
    marker. Pair with `try_begin()` on the start path — this catches a stale
    cross-process run, `try_begin()` closes the live-request race."""
    row = await db_read(lambda db: HarvestRunService(db).get_active_run())
    if row is not None:
        return {"job_id": row.job_id, "run_id": row.run_id, "source": row.source}
    return None


async def daily_budget_conflict() -> dict | None:
    """Global daily job cap: return `{"used", "limit"}` if today's harvested
    total has reached MAX_JOBS_PER_DAY, else None. Disabled when the limit is 0.

    Fail-open — if the DB read fails (`db_read` returns None) the run is allowed,
    matching the guard's best-effort posture (never block a run on a transient
    DB error). Counts finished runs since UTC midnight (see
    `HarvestRunService.jobs_scraped_today`)."""
    limit = get_settings().max_jobs_per_day
    if limit <= 0:
        return None
    used = await db_read(lambda db: HarvestRunService(db).jobs_scraped_today())
    if used is not None and used >= limit:
        return {"used": used, "limit": limit}
    return None


def try_begin(job_id: str | None, run_id: str, source: str | None) -> dict | None:
    """Atomically claim the in-process single-flight slot.

    Returns None (and marks the run in-flight) if no run is active in THIS
    process; otherwise returns the existing run's info and changes nothing.

    Contains NO `await`, so the check and the set can't be interleaved by the
    event loop — this is what actually closes the double-click / two-tab start
    race that the old `check_conflict()` + `begin()` pair left open (the DB read
    inside check_conflict is an await point both callers could slip past before
    either called begin). Must be paired with `end()` when it returns None."""
    global _active
    if _active is not None:
        return dict(_active)
    clear_stop()  # fresh run — discard any stop signal left over from a prior one
    _active = {"job_id": job_id, "run_id": run_id, "source": source}
    logger.info("run_guard_begin", job_id=job_id, run_id=run_id, source=source)
    return None


def begin(job_id: str | None, run_id: str, source: str | None) -> None:
    """Mark a run as in-flight in this process. Must be paired with end().

    Prefer `try_begin()` on the start path — it makes the check-and-set atomic.
    This unconditional setter remains for callers that have already established
    exclusivity by other means."""
    global _active
    _active = {"job_id": job_id, "run_id": run_id, "source": source}
    logger.info("run_guard_begin", job_id=job_id, run_id=run_id, source=source)


def end() -> None:
    """Clear the in-flight marker. Safe to call even if no run is active."""
    global _active
    if _active is not None:
        logger.info("run_guard_end", **_active)
    _active = None
    clear_stop()  # never leave a stop signal set once the run is over


@asynccontextmanager
async def single_flight(run_id: str, source: str | None, job_id: str | None = None):
    """Async context manager for a synchronous run (single-source routes, the
    scheduler): raises JobAlreadyRunningError (HTTP 409) if a harvest is already
    running, otherwise marks in-flight for the duration and releases on exit.

    The multi-source POST /run-harvest-agent can't use this — its run outlives
    the request in a background task — so it calls try_begin()/end() directly."""
    # Global daily cap first — reject before touching the single-flight slot.
    budget = await daily_budget_conflict()
    if budget is not None:
        raise DailyJobLimitExceededError(budget["used"], budget["limit"])
    # DB backstop next (a run left 'running' by a previous process), then the
    # atomic in-process claim. try_begin() serializes concurrent starts: even if
    # two callers both clear the DB check, only the first claims the slot; the
    # rest get 409.
    db = await db_conflict()
    if db is not None:
        raise JobAlreadyRunningError(db.get("job_id") or "", details=db)
    conflict = try_begin(job_id, run_id, source)
    if conflict is not None:
        raise JobAlreadyRunningError(conflict.get("job_id") or "", details=conflict)
    try:
        yield
    finally:
        end()
