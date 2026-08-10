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
from datetime import datetime
from typing import Any, Awaitable, Callable, TypeVar

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.dependencies import get_session_factory
from app.models.harvest_run import HarvestRunORM, LlmCallORM, ScrapedJobORM

logger = structlog.get_logger(__name__)

_T = TypeVar("_T")


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
        self._db.add_all(
            [
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
                    skills=j.get("skills") or [],
                    work_mode=j.get("work_mode", "not_specified"),
                    company_url=j.get("company_url", ""),
                    employment_type=j.get("employment_type", ""),
                    job_type=j.get("job_type", ""),
                    domain=j.get("domain", "Any"),
                    hiring_entity=j.get("hiring_entity", "Any"),
                    is_gcc=j.get("is_gcc", False),
                    verification_status=j.get("verification_status", "pending"),
                    job_poster_name=j.get("job_poster_name"),
                    job_poster_designation=j.get("job_poster_designation"),
                    linkedin_profile_url=j.get("linkedin_profile_url"),
                    current_company=j.get("current_company"),
                    email_id=j.get("email_id"),
                    contact_number=j.get("contact_number"),
                )
                for j in jobs
            ]
        )
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
