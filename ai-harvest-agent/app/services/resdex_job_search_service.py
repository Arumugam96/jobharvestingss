"""Per-job "Run Resdex search" on the harvest job detail page.

A harvest that ran while the Naukri recruiter was logged out still ran the LLM
requirements step and stored the job's T1–T3 queries (a ``queries_only`` search —
resdex_match_service). Once someone logs in, this searches Resdex for ONE such job
with exactly those stored queries — no new requirements LLM call, no email (the
outreach email already went out with the default no-Resdex template).

  start_job_search(job_id)   checks, claims the single-flight slot, starts the
                             background search; ResdexJobSearchError on a failed check
  is_running(job_id)         True while that job's search is in flight (the detail
                             page polls GET /jobs/{id}/outreach-detail until it ends)

The search drives the same recruiter browser and draws on the same daily Resdex
quota as a harvest, so it holds the run_guard single-flight slot like a Demand
Search re-run: it never runs beside a harvest or a Demand batch.
"""
from __future__ import annotations

import asyncio
from typing import Any

import structlog
from sqlalchemy import select

from app.config import get_settings
from app.core.job_url import normalize_job_url
from app.core.tenant_context import apply_tenant
from app.models.resdex import ResdexSearchORM, ResdexSearchStatus
from app.services import resdex_match_service as rms
from app.services import run_guard
from app.services.harvest_run_service import DEMAND_SOURCE, HarvestRunService, db_read

logger = structlog.get_logger(__name__)

# Latest-search outcomes where Resdex was NOT (fully) searched — the job page offers
# "Run Resdex search" for these. matched / no_results / skipped_no_jd are final.
RUNNABLE_STATUSES = (
    ResdexSearchStatus.QUERIES_ONLY,
    ResdexSearchStatus.SESSION_EXPIRED,
    ResdexSearchStatus.SESSION_BUSY,
    ResdexSearchStatus.CAP_REACHED,
    ResdexSearchStatus.ERROR,
)

# run_guard source of a per-job search (shown by GET /active-run like any run).
RUN_GUARD_SOURCE = "Resdex"

# Strong refs so a fire-and-forget search task isn't garbage-collected mid-run.
_tasks: set[asyncio.Task] = set()


class ResdexJobSearchError(Exception):
    """A precondition failed — the route turns it into an HTTP error whose detail
    is {code, message} (the job page keys its message on ``code``)."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    def to_detail(self) -> dict:
        return {"code": self.code, "message": self.message}


def _guard_run_id(job_id: str) -> str:
    return f"resdex_{job_id}"


def is_running(job_id: str) -> bool:
    """True while this job's per-job Resdex search holds the single-flight slot."""
    active = run_guard.active_run() or {}
    return active.get("source") == RUN_GUARD_SOURCE and active.get("run_id") == _guard_run_id(job_id)


def can_run(status: str | None) -> bool:
    return (status or "") in RUNNABLE_STATUSES


def _recruiter_session() -> dict:
    """The recorded Naukri recruiter check (no browser launch)."""
    from app.agents.naukri_recruiter_agent import read_status

    st = read_status()
    return {"logged_in": bool(st.get("logged_in")),
            "login_window_open": bool(st.get("login_window_open"))}


async def _job_and_latest_search(job_id: str) -> tuple[Any, Any]:
    """(ScrapedJobORM, its newest ResdexSearchORM | None) for this tenant, or (None, None)."""
    async def _q(db):
        job = await HarvestRunService(db).get_scraped_job_by_id(job_id)
        if job is None:
            return None, None
        key = normalize_job_url(job.job_url)
        if not key:
            return job, None
        search = (await db.execute(
            apply_tenant(select(ResdexSearchORM), ResdexSearchORM.tenant_id)
            .where(ResdexSearchORM.job_url == key)
            .order_by(ResdexSearchORM.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        return job, search
    return await db_read(_q) or (None, None)


async def start_job_search(job_id: str) -> dict:
    """Start the Resdex search for one harvest job from its stored queries, in a
    detached task under the single-flight slot. Returns {job_id, status: "running"};
    raises ResdexJobSearchError (404 / 409) when a check fails."""
    if not get_settings().resdex_enabled:
        raise ResdexJobSearchError(
            409, "resdex_disabled",
            "Resdex is disabled on this server (RESDEX_ENABLED=false).",
        )
    if is_running(job_id):
        return {"job_id": job_id, "status": "running"}
    job, search = await _job_and_latest_search(job_id)
    if job is None:
        raise ResdexJobSearchError(404, "not_found", f"Job '{job_id}' not found.")
    if (job.source or "").lower() == DEMAND_SOURCE.lower():
        raise ResdexJobSearchError(
            409, "demand_job",
            "This is a Demand Search job — use “Re-run Resdex search” on its Demand Search page.",
        )
    if search is None or not isinstance(search.requirements, dict) or not search.requirements:
        raise ResdexJobSearchError(
            409, "no_saved_queries",
            "This job has no saved Resdex queries — it was not picked for a Resdex search.",
        )
    if not can_run(search.status):
        raise ResdexJobSearchError(
            409, "already_searched", "Resdex was already searched for this job.",
        )
    sess = _recruiter_session()
    if sess["login_window_open"]:
        raise ResdexJobSearchError(
            409, "session_busy",
            "A Naukri recruiter login window is open. Finish logging in, then try again.",
        )
    if not sess["logged_in"]:
        raise ResdexJobSearchError(
            409, "recruiter_logged_out",
            "The Naukri recruiter session is logged out. Log in, then run the search again.",
        )

    gid = _guard_run_id(job_id)
    conflict = await run_guard.db_conflict()
    if conflict is not None or run_guard.try_begin(None, gid, RUN_GUARD_SOURCE) is not None:
        raise ResdexJobSearchError(
            409, "harvest_running",
            "A harvest or Demand Search is running and is using the recruiter browser. "
            "Run this search after it finishes.",
        )
    try:
        # Plain values only — the ORM rows' session is closed by now.
        kwargs = dict(
            job_url=search.job_url, run_id=search.run_id or "",
            target={"job_title": search.job_title or job.job_title or "",
                    "company": search.company or job.company or "",
                    "contact_origin": search.contact_source or ""},
            requirements=dict(search.requirements),
            tier_queries=list(search.tier_queries) if isinstance(search.tier_queries, list)
            else None,
        )
        task = asyncio.create_task(_background(job_id, kwargs), name=f"resdex-job-{job_id}")
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    except Exception:
        run_guard.end()          # the task never started → its finally won't release
        raise
    logger.info("resdex_job_search_started", job_id=job_id, job_url=search.job_url,
                previous_status=search.status)
    return {"job_id": job_id, "status": "running"}


async def _background(job_id: str, kwargs: dict) -> None:
    """Always releases the single-flight slot. run_saved_search never raises."""
    try:
        status = await rms.run_saved_search(**kwargs)
        logger.info("resdex_job_search_finished", job_id=job_id, status=status)
    finally:
        run_guard.end()
