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

from contextlib import asynccontextmanager

import structlog

from app.core.exceptions import JobAlreadyRunningError
from app.services.harvest_run_service import HarvestRunService, db_read

logger = structlog.get_logger(__name__)

# {"job_id", "run_id", "source"} while a run is in flight in THIS process; None otherwise.
_active: dict | None = None


def active_run() -> dict | None:
    """The run this process currently considers in-flight, or None."""
    return dict(_active) if _active else None


async def check_conflict() -> dict | None:
    """Return conflict info if a harvest is already running (in-process marker
    first, then the DB backstop), else None. Call `begin()` only when this
    returns None."""
    if _active is not None:
        return dict(_active)
    row = await db_read(lambda db: HarvestRunService(db).get_active_run())
    if row is not None:
        return {"job_id": row.job_id, "run_id": row.run_id, "source": row.source}
    return None


def begin(job_id: str | None, run_id: str, source: str | None) -> None:
    """Mark a run as in-flight in this process. Must be paired with end()."""
    global _active
    _active = {"job_id": job_id, "run_id": run_id, "source": source}
    logger.info("run_guard_begin", job_id=job_id, run_id=run_id, source=source)


def end() -> None:
    """Clear the in-flight marker. Safe to call even if no run is active."""
    global _active
    if _active is not None:
        logger.info("run_guard_end", **_active)
    _active = None


@asynccontextmanager
async def single_flight(run_id: str, source: str | None, job_id: str | None = None):
    """Async context manager for a synchronous run (single-source routes, the
    scheduler): raises JobAlreadyRunningError (HTTP 409) if a harvest is already
    running, otherwise marks in-flight for the duration and releases on exit.

    The multi-source POST /run-harvest-agent can't use this — its run outlives
    the request in a background task — so it calls begin()/end() directly."""
    conflict = await check_conflict()
    if conflict is not None:
        raise JobAlreadyRunningError(conflict.get("job_id") or "", details=conflict)
    begin(job_id, run_id, source)
    try:
        yield
    finally:
        end()
