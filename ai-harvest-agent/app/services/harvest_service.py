"""Business logic for harvest job CRUD and lifecycle management."""
from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import JobAlreadyRunningError, JobNotFoundError
from app.models.harvest import (
    HarvestJob,
    HarvestJobCreate,
    HarvestJobORM,
    HarvestJobWithResults,
    HarvestResult,
    HarvestResultORM,
    JobStatus,
)

logger = structlog.get_logger(__name__)


class HarvestService:
    """CRUD + lifecycle operations for HarvestJob entities."""

    def __init__(self, db: AsyncSession) -> None:
        """Bind to the caller's session; methods flush/execute but never commit."""
        self._db = db

    # ── Create ───────────────────────────────────────────────────────────────────

    async def create_job(self, payload: HarvestJobCreate) -> HarvestJob:
        """Insert a new PENDING job with a fresh UUID (flushed, not committed)."""
        job = HarvestJobORM(
            id=str(uuid.uuid4()),
            url=payload.url,
            goal=payload.goal,
            agent_type=payload.agent_type,
            max_pages=payload.max_pages,
            output_schema=payload.output_schema,
            status=JobStatus.PENDING,
        )
        self._db.add(job)
        await self._db.flush()
        logger.info("job_created", job_id=job.id, url=job.url)
        return HarvestJob.model_validate(job)

    # ── Read ─────────────────────────────────────────────────────────────────────

    async def get_job(self, job_id: str) -> HarvestJob:
        """The job by id; raises JobNotFoundError (404) when missing."""
        orm = await self._get_or_raise(job_id)
        return HarvestJob.model_validate(orm)

    async def get_job_with_results(self, job_id: str) -> HarvestJobWithResults:
        """The job plus its per-page results; raises JobNotFoundError when missing."""
        orm = await self._get_or_raise(job_id)
        return HarvestJobWithResults.model_validate(orm)

    async def list_jobs(
        self,
        status: JobStatus | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> list[HarvestJob]:
        """One page (1-based) of jobs, newest first, optionally filtered by status."""
        stmt = select(HarvestJobORM).order_by(HarvestJobORM.created_at.desc())
        if status:
            stmt = stmt.where(HarvestJobORM.status == status)
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        result = await self._db.execute(stmt)
        return [HarvestJob.model_validate(r) for r in result.scalars()]

    # ── Update ───────────────────────────────────────────────────────────────────

    async def mark_running(self, job_id: str) -> HarvestJob:
        """Move the job to RUNNING; raises JobAlreadyRunningError if it already is.

        Raises JobNotFoundError when the job does not exist.
        """
        orm = await self._get_or_raise(job_id)
        if orm.status == JobStatus.RUNNING:
            raise JobAlreadyRunningError(job_id)
        await self._db.execute(
            update(HarvestJobORM)
            .where(HarvestJobORM.id == job_id)
            .values(status=JobStatus.RUNNING)
        )
        orm.status = JobStatus.RUNNING
        return HarvestJob.model_validate(orm)

    async def mark_completed(self, job_id: str) -> HarvestJob:
        """Set status COMPLETED and return the refreshed job."""
        return await self._set_status(job_id, JobStatus.COMPLETED)

    async def mark_failed(self, job_id: str, error: str) -> HarvestJob:
        """Set status FAILED with ``error`` as the error_message; return the refreshed job."""
        await self._db.execute(
            update(HarvestJobORM)
            .where(HarvestJobORM.id == job_id)
            .values(status=JobStatus.FAILED, error_message=error)
        )
        return await self.get_job(job_id)

    async def mark_cancelled(self, job_id: str) -> HarvestJob:
        """Set status CANCELLED and return the refreshed job."""
        return await self._set_status(job_id, JobStatus.CANCELLED)

    # ── Results ──────────────────────────────────────────────────────────────────

    async def add_result(
        self,
        job_id: str,
        page_url: str,
        page_number: int,
        raw_content: str | None = None,
        extracted_data: dict[str, Any] | None = None,
        screenshot_path: str | None = None,
    ) -> HarvestResult:
        """Store one scraped page's content/extraction for a job (flushed, not committed)."""
        result_orm = HarvestResultORM(
            id=str(uuid.uuid4()),
            job_id=job_id,
            page_url=page_url,
            page_number=page_number,
            raw_content=raw_content,
            extracted_data=extracted_data,
            screenshot_path=screenshot_path,
        )
        self._db.add(result_orm)
        await self._db.flush()
        logger.info("result_saved", job_id=job_id, page_url=page_url)
        return HarvestResult.model_validate(result_orm)

    # ── Delete ───────────────────────────────────────────────────────────────────

    async def delete_job(self, job_id: str) -> None:
        """Delete the job; raises JobNotFoundError when it does not exist."""
        orm = await self._get_or_raise(job_id)
        await self._db.delete(orm)
        logger.info("job_deleted", job_id=job_id)

    # ── Helpers ──────────────────────────────────────────────────────────────────

    async def _get_or_raise(self, job_id: str) -> HarvestJobORM:
        """Load the job ORM row or raise JobNotFoundError."""
        result = await self._db.execute(
            select(HarvestJobORM).where(HarvestJobORM.id == job_id)
        )
        orm = result.scalar_one_or_none()
        if orm is None:
            raise JobNotFoundError(job_id)
        return orm

    async def _set_status(self, job_id: str, status: JobStatus) -> HarvestJob:
        """UPDATE the status column, then re-read the job (JobNotFoundError if missing)."""
        await self._db.execute(
            update(HarvestJobORM).where(HarvestJobORM.id == job_id).values(status=status)
        )
        return await self.get_job(job_id)
