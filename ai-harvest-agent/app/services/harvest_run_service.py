"""Persistence for harvest workflow runs, scraped jobs, and LLM call audit logs.

Covers the LinkedIn/Naukri/Dice job-board pipeline — both the multi-source
orchestrator flow (POST /run-harvest-agent) and the three single-source flows
(POST /run-{linkedin,naukri,dice}-agent). Writes here are additive alongside
the existing JSON/Excel file storage (JobTracker, RunHistoryService, the
per-source *StorageService classes keep running unchanged); the DB becomes
what the frontend-facing GET endpoints read from.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TypeVar

import structlog
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from app.config import get_settings
from app.core.dependencies import get_session_factory
from app.models.harvest_run import HarvestRunORM, LlmCallORM, ScrapedJobORM
from app.services.recruiter_service import upsert_recruiter

logger = structlog.get_logger(__name__)

_T = TypeVar("_T")

# Shared with app/routes/frontend_routes.py's GET /jobs — one canonical set so
# the DB-side ORDER BY and the JSON-fallback path's in-memory sort never drift.
JOB_SORT_FIELDS = {
    "posted_date", "company", "job_title", "source", "hiring_entity", "location",
}


def data_source_mode() -> str:
    """Current value of DATA_SOURCE from .env — "auto" (default) | "database"
    | "json". See Settings.data_source in app/config.py for what each means."""
    return get_settings().data_source


async def resolve_read(
    mode: str,
    db_fn: Callable[[], Awaitable[_T]],
    json_fn: Callable[[], Any],
) -> tuple[Any, str]:
    """Pick DB vs. JSON for one GET-endpoint read, per the DATA_SOURCE setting.

    "json"     -> never call db_fn; always read the JSON/file store.
    "database" -> only call db_fn; caller gets whatever it returns (including
                  an empty/None "not found") without a JSON fallback.
    "auto"     -> call db_fn first; fall back to json_fn if it returned
                  nothing (None, [], falsy) — today's pre-toggle behavior.

    Returns (result, source_used) so the caller knows which shape it's
    holding — a DB row/ORM list needs its own mapper, a JSON result is
    already in the response's final shape.
    """
    if mode == "json":
        return json_fn(), "json"
    result = await db_fn()
    if result or mode == "database":
        return result, "database"
    return json_fn(), "json"


async def db_write(coro_fn: Callable[[AsyncSession], Awaitable[_T]]) -> _T | None:
    """Best-effort DB mirror for the harvest workflow.

    A DB outage must never break the file-based flow that already works
    today, so any exception here is logged and swallowed. Acquires its own
    short-lived session via get_session_factory() so it works both inside a
    request (alongside the usual Depends(get_db_session)) and from a detached
    asyncio.create_task, which has no FastAPI dependency injection available.
    """
    try:
        session_factory = get_session_factory(get_settings())
        async with session_factory() as db:
            try:
                result = await coro_fn(db)
                await db.commit()
                return result
            except Exception:
                await db.rollback()
                raise
    except Exception as exc:
        logger.warning("harvest_db_mirror_failed", error=str(exc))
        return None


async def db_read(coro_fn: Callable[[AsyncSession], Awaitable[_T]]) -> _T | None:
    """Best-effort DB read for the frontend-facing GET endpoints. Returns None
    on any DB error (unreachable DB, etc.) rather than raising, so callers can
    fall back to the pre-existing file-based read for that endpoint."""
    try:
        session_factory = get_session_factory(get_settings())
        async with session_factory() as db:
            return await coro_fn(db)
    except Exception as exc:
        logger.warning("harvest_db_read_failed", error=str(exc))
        return None


class HarvestRunService:
    """CRUD for HarvestRunORM / ScrapedJobORM / LlmCallORM."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Write ────────────────────────────────────────────────────────────────

    async def create_run(
        self,
        run_id: str,
        job_id: str | None = None,
        source: str | None = None,
        sources: list[str] | None = None,
        filters_snapshot: dict[str, Any] | None = None,
        started_at: datetime | None = None,
    ) -> str:
        """Create a new run row. source=None for a multi-source orchestrator
        run; a source name ("LinkedIn"/"Naukri"/"Dice") for a standalone
        single-source run. Returns the new row's surrogate id."""
        run = HarvestRunORM(
            id=str(uuid.uuid4()),
            job_id=job_id,
            run_id=run_id,
            source=source,
            sources=sources if sources is not None else ([source] if source else []),
            filters_snapshot=filters_snapshot,
            started_at=started_at,
        )
        self._db.add(run)
        await self._db.flush()
        return run.id

    async def update_run(self, run_pk: str, **fields: Any) -> None:
        if not fields:
            return
        await self._db.execute(
            update(HarvestRunORM).where(HarvestRunORM.id == run_pk).values(**fields)
        )

    async def bulk_insert_scraped_jobs(self, run_pk: str, jobs: list[dict[str, Any]]) -> None:
        """jobs is a list of plain dicts keyed by ScrapedJobORM's column names
        (e.g. UnifiedJob.to_dict()'s output, or a per-source mapper's dict for
        the standalone single-source routes — see linkedin_routes.py's
        _to_scraped_job_dict() for the field-name translation each source
        needs, since LinkedIn/Naukri/Dice's own scraped dataclasses don't
        share field names for recruiter/poster info)."""
        if not jobs:
            return
        rows: list[ScrapedJobORM] = []
        for j in jobs:
            recruiter_id = None
            poster_name = (j.get("job_poster_name") or "").strip()
            if poster_name:
                recruiter = await upsert_recruiter(
                    self._db,
                    person_name=poster_name,
                    company_name=j.get("current_company") or j.get("company") or "",
                    designation=j.get("job_poster_designation") or "",
                    linkedin_profile_url=j.get("linkedin_profile_url"),
                    harvest_source=j.get("source", ""),
                )
                recruiter_id = recruiter.id if recruiter else None
            rows.append(
                ScrapedJobORM(
                    id=str(uuid.uuid4()),
                    run_id=run_pk,
                    source=j.get("source", ""),
                    job_title=j.get("job_title", ""),
                    company=j.get("company", ""),
                    location=j.get("location", ""),
                    salary=j.get("salary", ""),
                    experience=j.get("experience", ""),
                    posted_date=j.get("posted_date", ""),
                    job_url=j.get("job_url", ""),
                    job_description=j.get("job_description", ""),
                    job_description_html=j.get("job_description_html", ""),
                    skills=j.get("skills") or [],
                    work_mode=j.get("work_mode", "not_specified"),
                    company_url=j.get("company_url", ""),
                    employment_type=j.get("employment_type", ""),
                    job_type=j.get("job_type", ""),
                    domain=j.get("domain", "Any"),
                    hiring_entity=j.get("hiring_entity", "Any"),
                    is_gcc=j.get("is_gcc", False),
                    verification_status=j.get("verification_status", "pending"),
                    passed_filter=j.get("passed_filter", True),
                    filter_reason=j.get("filter_reason", ""),
                    job_poster_name=j.get("job_poster_name"),
                    job_poster_designation=j.get("job_poster_designation"),
                    linkedin_profile_url=j.get("linkedin_profile_url"),
                    current_company=j.get("current_company"),
                    email_id=j.get("email_id"),
                    contact_number=j.get("contact_number"),
                    recruiter_id=recruiter_id,
                )
            )
        self._db.add_all(rows)
        await self._db.flush()

    async def bulk_insert_llm_calls(self, run_pk: str, calls: list[dict[str, Any]]) -> None:
        if not calls:
            return
        self._db.add_all(
            [
                LlmCallORM(
                    id=str(uuid.uuid4()),
                    run_id=run_pk,
                    job_url=c.get("job_url"),
                    provider=c["provider"],
                    model=c["model"],
                    prompt=c["prompt"],
                    response=c.get("response"),
                    prompt_chars=c.get("prompt_chars", 0),
                    response_chars=c.get("response_chars", 0),
                    input_tokens=c.get("input_tokens"),
                    output_tokens=c.get("output_tokens"),
                    latency_ms=c.get("latency_ms"),
                    success=c.get("success", True),
                    error_message=c.get("error_message"),
                    retry_count=c.get("retry_count", 0),
                )
                for c in calls
            ]
        )
        await self._db.flush()

    # ── Read ─────────────────────────────────────────────────────────────────

    async def get_by_job_id(self, job_id: str) -> HarvestRunORM | None:
        """Look up an orchestrator run by its ephemeral poll id (GET /harvest-status/{job_id})."""
        result = await self._db.execute(select(HarvestRunORM).where(HarvestRunORM.job_id == job_id))
        return result.scalar_one_or_none()

    async def get_by_run_id(self, run_id: str, source: str | None = None) -> HarvestRunORM | None:
        """source=None matches orchestrator runs only; a source name matches
        that source's standalone runs only — mirrors list_runs()'s filter."""
        stmt = select(HarvestRunORM).where(HarvestRunORM.run_id == run_id)
        stmt = stmt.where(HarvestRunORM.source.is_(None) if source is None else HarvestRunORM.source == source)
        result = await self._db.execute(stmt.order_by(HarvestRunORM.created_at.desc()))
        return result.scalars().first()

    async def list_runs(self, source: str | None = None, limit: int = 50) -> list[HarvestRunORM]:
        stmt = select(HarvestRunORM)
        stmt = stmt.where(HarvestRunORM.source.is_(None) if source is None else HarvestRunORM.source == source)
        stmt = stmt.order_by(HarvestRunORM.created_at.desc()).limit(limit)
        result = await self._db.execute(stmt)
        return list(result.scalars())

    async def fail_stale_running(self, message: str = "Interrupted by a server restart") -> int:
        """Mark every still-'running' run as 'failed'. A harvest executes in a
        detached asyncio task that does NOT survive a process restart, so any
        row left 'running' at startup is stale. Without this the single-flight
        guard's DB backstop (get_active_run) would see a dead run and reject
        every new start with 409 forever. Mirrors JobTracker.load_from_disk's
        in-memory reconciliation. Returns the number of rows updated."""
        result = await self._db.execute(
            update(HarvestRunORM)
            .where(HarvestRunORM.status == "running")
            .values(status="failed", message=message, error=message, progress=100)
        )
        return result.rowcount or 0

    async def get_active_run(self) -> HarvestRunORM | None:
        """The most-recent still-running harvest (any source, orchestrator or
        standalone), or None. Backs the single-flight guard: Playwright can't
        drive the same Chrome profile from two runs at once, so a second start
        while this returns non-None must be rejected. This is the cross-process
        backstop to the in-process asyncio.Lock in the start routes."""
        stmt = (
            select(HarvestRunORM)
            .options(noload(HarvestRunORM.jobs))  # guard only needs identity — don't load child jobs
            .where(HarvestRunORM.status == "running")
            .order_by(HarvestRunORM.started_at.desc())
            .limit(1)
        )
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def jobs_scraped_today(self) -> int:
        """Total jobs harvested across all runs since UTC midnight — backs the
        global daily cap (MAX_JOBS_PER_DAY). Sums `combined_count` (the deduped
        per-run total), which is written at run completion, so this reflects
        finished runs; the single-flight guard ensures the prior run has ended
        (and been counted) before a new one can start. `created_at` is used
        (never null); the boundary is naive UTC midnight to match SQLite's
        naive-UTC storage of the column."""
        start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
        result = await self._db.execute(
            select(func.coalesce(func.sum(HarvestRunORM.combined_count), 0))
            .where(HarvestRunORM.created_at >= start)
        )
        return int(result.scalar_one() or 0)

    async def list_scraped_jobs(
        self,
        *,
        keyword:       str | None = None,
        company:       str | None = None,
        source:        str | None = None,
        hiring_entity: str | None = None,
        work_mode:     str | None = None,
        date_from:     str | None = None,
        date_to:       str | None = None,
        run_id:        str | None = None,
        sort_by:       str = "posted_date",
        sort_order:    str = "desc",
        page:          int = 1,
        page_size:     int = 50,
    ) -> tuple[list[ScrapedJobORM], int]:
        """ScrapedJobORM rows across *every* run (unlike get_by_run_id/
        list_runs, not scoped to one HarvestRunORM) — the DB-backed
        equivalent of frontend_routes.py's _apply_job_filters/_apply_sort/
        _paginate against the JSON file store. Filtering/sorting/pagination
        all happen in SQL so this scales as scraped_jobs accumulates across
        runs (the JSON file only ever held the latest run's jobs).

        posted_date is a free-text String column, not a real DateTime — the
        date_from/date_to bound is a string-prefix comparison, matching the
        JSON path's _date_gte/_date_lte, and only works if callers persist
        posted_date as a YYYY-MM-DD-prefixed string (true for LinkedIn's
        _format_posted(); not independently verified for Naukri/Dice)."""
        stmt = select(ScrapedJobORM)
        if keyword:
            like = f"%{keyword}%"
            stmt = stmt.where(
                or_(
                    ScrapedJobORM.job_title.ilike(like),
                    ScrapedJobORM.job_description.ilike(like),
                    ScrapedJobORM.company.ilike(like),
                )
            )
        if company:
            stmt = stmt.where(ScrapedJobORM.company.ilike(f"%{company}%"))
        if source:
            stmt = stmt.where(func.lower(ScrapedJobORM.source) == source.lower())
        if hiring_entity:
            stmt = stmt.where(func.lower(ScrapedJobORM.hiring_entity) == hiring_entity.lower())
        if work_mode:
            stmt = stmt.where(func.lower(ScrapedJobORM.work_mode) == work_mode.lower())
        if date_from:
            stmt = stmt.where(func.substr(ScrapedJobORM.posted_date, 1, 10) >= date_from[:10])
        if date_to:
            stmt = stmt.where(func.substr(ScrapedJobORM.posted_date, 1, 10) <= date_to[:10])
        if run_id:
            # ScrapedJobORM.run_id is the HarvestRunORM PK (uuid); the caller
            # passes the human-facing display run_id (e.g. "20260817_110703"),
            # so resolve it through a subquery. A display run_id can map to more
            # than one HarvestRunORM row (the orchestrator run plus any
            # single-source runs sharing the timestamp), hence IN (...).
            stmt = stmt.where(
                ScrapedJobORM.run_id.in_(
                    select(HarvestRunORM.id).where(HarvestRunORM.run_id == run_id)
                )
            )

        total = (
            await self._db.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()

        sort_column = getattr(ScrapedJobORM, sort_by if sort_by in JOB_SORT_FIELDS else "posted_date")
        stmt = stmt.order_by(sort_column.desc() if sort_order.lower() == "desc" else sort_column.asc())
        stmt = stmt.offset(max(0, page - 1) * page_size).limit(page_size)

        result = await self._db.execute(stmt)
        return list(result.scalars()), total

    async def get_scraped_job_by_id(self, job_id: str) -> ScrapedJobORM | None:
        result = await self._db.execute(select(ScrapedJobORM).where(ScrapedJobORM.id == job_id))
        return result.scalar_one_or_none()

    async def list_jobs_for_run(self, run_pk: str) -> list[ScrapedJobORM]:
        """All of one run's scraped jobs — used to build the post-harvest
        report (JSON/Excel) from the DB instead of harvest-time files."""
        result = await self._db.execute(
            select(ScrapedJobORM).where(ScrapedJobORM.run_id == run_pk)
        )
        return list(result.scalars())

    async def list_all_jobs_for_report(self) -> list[ScrapedJobORM]:
        """Every scraped job on record, newest posting first — the dataset
        behind GET /download/{json,excel}, matching what GET /jobs lists."""
        result = await self._db.execute(
            select(ScrapedJobORM).order_by(ScrapedJobORM.posted_date.desc())
        )
        return list(result.scalars())


# ── Shared read-side view helpers ───────────────────────────────────────────────
# Used by linkedin_routes.py / naukri_routes.py / dice_routes.py to reconstruct
# the exact same JSON shape their *StorageService JSON files already produce,
# so switching the read source from file to DB is invisible to the frontend.

def filters_view(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """The 13-key filter subset every source route's _build_payload() embeds
    in its saved JSON (identical shape across linkedin/naukri/dice_routes.py)."""
    s = snapshot or {}
    return {
        "keyword": s.get("keyword", ""),
        "location": s.get("location", ""),
        "job_type": s.get("job_type", ""),
        "work_mode": s.get("work_mode", ""),
        "search_window_hours": s.get("search_window_hours", 0),
        "max_jobs": s.get("max_jobs", 0),
        "domain": s.get("domain", ""),
        "hiring_entity": s.get("hiring_entity", ""),
        "gcc_mode": s.get("gcc_mode", ""),
        "salary_min": s.get("salary_min"),
        "salary_max": s.get("salary_max"),
        "salary_currency": s.get("salary_currency", ""),
        "include_undisclosed_salary": s.get("include_undisclosed_salary", False),
    }


def scraped_job_view(job: ScrapedJobORM) -> dict[str, Any]:
    """Matches the shape of one entry in a combined-JSON file's "jobs" list
    (see frontend_routes.py's _load_all_jobs) — used by GET /jobs so the DB
    and JSON read paths are interchangeable to the frontend. `id` here is
    the row's real primary key; a JSON-sourced job instead gets a synthetic
    md5-of-url id from frontend_routes.py's _job_id() — the two id schemes
    only ever coexist across responses, never mixed within one.

    email_id/contact_number merge in the linked RecruiterORM's enriched
    contact info (official_email_id/contact_number) when the job row itself
    scraped none — one view feeds the UI, the report files, and the report
    email, so they all show the merged contact identically."""
    recruiter = job.recruiter
    email = job.email_id or (recruiter.official_email_id if recruiter else "") or None
    phone = job.contact_number or (recruiter.contact_number if recruiter else "") or None
    return {
        "id":                     job.id,
        "job_title":              job.job_title,
        "company":                job.company,
        "location":               job.location,
        "salary":                 job.salary,
        "experience":             job.experience,
        "posted_date":            job.posted_date,
        "job_url":                job.job_url,
        "job_description":        job.job_description,
        "job_description_html":   job.job_description_html,
        "skills":                 job.skills,
        "work_mode":              job.work_mode,
        "company_url":            job.company_url,
        "employment_type":        job.employment_type,
        "job_type":               job.job_type,
        "domain":                 job.domain,
        "hiring_entity":          job.hiring_entity,
        "is_gcc":                 job.is_gcc,
        "verification_status":    job.verification_status,
        "passed_filter":          job.passed_filter,
        "filter_reason":          job.filter_reason,
        "source":                 job.source,
        "job_poster_name":        job.job_poster_name,
        "job_poster_designation": job.job_poster_designation,
        "linkedin_profile_url":   job.linkedin_profile_url,
        "current_company":        job.current_company,
        "email_id":               email,
        "contact_number":         phone,
    }


def run_to_result_summary(run: HarvestRunORM) -> dict[str, Any]:
    """Matches every *StorageService.list_results() summary row shape:
    {run_id, executed_at, status, total_found, source, file_path}."""
    return {
        "run_id": run.run_id,
        "executed_at": run.started_at.isoformat() if run.started_at else "",
        "status": run.status,
        "total_found": run.combined_count,
        "source": run.source or "",
        "file_path": run.json_path or "",
    }
