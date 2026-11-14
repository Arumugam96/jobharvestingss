"""Harvest job CRUD and async execution endpoints."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.core.dependencies import (
    get_db_session,
    get_llm_service,
    get_playwright_service,
    get_session_factory,
)
from app.core.security import require_api_key
from app.models.agent import AgentConfig, AgentType
from app.models.harvest import HarvestJob, HarvestJobCreate, HarvestJobWithResults, JobStatus
from app.models.response import APIResponse
from app.services.harvest_service import HarvestService
from app.services.llm_service import LLMService
from app.services.playwright_service import PlaywrightService
from app.agents.orchestrator import AgentOrchestrator

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post(
    "/start",
    response_model=APIResponse[HarvestJob],
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a new harvest job",
)
async def start_harvest(
    payload: HarvestJobCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db_session),
    llm: LLMService = Depends(get_llm_service),
    playwright: PlaywrightService = Depends(get_playwright_service),
) -> APIResponse[HarvestJob]:
    """Create a harvest job and run the matching agent on it in a background task.

    Returns the newly created job immediately (202). The job row is committed before the
    response so the background run can see it. The background run uses its OWN DB session
    (the request-scoped one is closed once the response is sent): it marks the job running,
    stores the agent's output as a single page-1 result, then marks it completed, or failed
    with the exception text, committing after each status change.
    """
    svc = HarvestService(db)
    job = await svc.create_job(payload)
    await db.commit()  # visible to the background task's separate session
    # Same engine as the request session (the shared app engine in production).
    session_factory = (
        async_sessionmaker(db.bind, expire_on_commit=False)
        if db.bind is not None else get_session_factory(get_settings())
    )

    async def _run_job() -> None:
        """Background body: run the orchestrator for this job and record result/failure
        through a fresh session of its own (same engine as the request).

        Any orchestrator exception is stored on the job as a failure message.
        """
        async with session_factory() as bg_db:
            bg_svc = HarvestService(bg_db)
            try:
                await bg_svc.mark_running(job.id)
                await bg_db.commit()
                orchestrator = AgentOrchestrator(llm, playwright)
                result = await orchestrator.run(
                    job_id=job.id,
                    url=payload.url,
                    goal=payload.goal,
                    config=AgentConfig(
                        agent_type=AgentType(payload.agent_type),
                        max_iterations=payload.max_pages * 3,
                    ),
                )
                await bg_svc.add_result(
                    job_id=job.id,
                    page_url=payload.url,
                    page_number=1,
                    extracted_data=result,
                )
                await bg_svc.mark_completed(job.id)
                await bg_db.commit()
            except Exception as exc:
                await bg_db.rollback()
                await bg_svc.mark_failed(job.id, str(exc))
                await bg_db.commit()

    background_tasks.add_task(_run_job)
    return APIResponse(data=job, message="Harvest job queued")


@router.get(
    "/{job_id}",
    response_model=APIResponse[HarvestJob],
    summary="Get harvest job status",
)
async def get_job(
    job_id: str,
    db: AsyncSession = Depends(get_db_session),
) -> APIResponse[HarvestJob]:
    """Return a harvest job's metadata and status; raises JobNotFoundError for an unknown id."""
    svc = HarvestService(db)
    job = await svc.get_job(job_id)
    return APIResponse(data=job)


@router.get(
    "/{job_id}/results",
    response_model=APIResponse[HarvestJobWithResults],
    summary="Get full job results including extracted data",
)
async def get_job_results(
    job_id: str,
    db: AsyncSession = Depends(get_db_session),
) -> APIResponse[HarvestJobWithResults]:
    """Return a harvest job together with all its extracted page results.

    Raises JobNotFoundError for an unknown id.
    """
    svc = HarvestService(db)
    job = await svc.get_job_with_results(job_id)
    return APIResponse(data=job)


@router.get(
    "",
    response_model=APIResponse[list[HarvestJob]],
    summary="List all harvest jobs",
)
async def list_jobs(
    status: JobStatus | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db_session),
) -> APIResponse[list[HarvestJob]]:
    """List harvest jobs newest first, optionally filtered by `status`; paginated."""
    svc = HarvestService(db)
    jobs = await svc.list_jobs(status=status, page=page, page_size=page_size)
    return APIResponse(data=jobs)


@router.delete(
    "/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel or delete a harvest job",
    response_class=Response,
)
async def delete_job(
    job_id: str,
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    """Delete a harvest job (does not stop a run already in progress); 204 on success.

    Raises JobNotFoundError for an unknown id.
    """
    svc = HarvestService(db)
    await svc.delete_job(job_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
