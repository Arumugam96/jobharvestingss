"""
Job self-assignment routes ("Assign to me" on the Harvested Jobs table).

Endpoints
─────────
POST   /jobs/{id}/assign — claim the job for the current user (optional note);
                           409 if someone else already holds it
DELETE /jobs/{id}/assign — release the job; only the assignee may do this

Both return {"job_id", "assigned_to"} where assigned_to is
{email, name, assigned_at, note} or null. See job_assignment_service.
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Body, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user, get_db_session
from app.models.auth import AuthenticatedUser
from app.services.job_assignment_service import NOTE_MAX_LEN, assign_job, assignment_view, unassign_job

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Jobs & Leads"])


class AssignJobRequest(BaseModel):
    note: str = Field(default="", max_length=NOTE_MAX_LEN, description="Optional comment shown with the assignment.")


@router.post(
    "/jobs/{job_id}/assign",
    status_code=status.HTTP_200_OK,
    summary="Assign a job to me",
    responses={404: {"description": "Job not found"}, 409: {"description": "Assigned to someone else"}},
)
async def assign_job_to_me(
    job_id: str,
    body: AssignJobRequest = Body(default_factory=AssignJobRequest),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    job = await assign_job(db, job_id, current_user, body.note)
    logger.info("job_assigned", job_id=job_id, user=current_user.email)
    return {"job_id": job_id, "assigned_to": assignment_view(job)}


@router.delete(
    "/jobs/{job_id}/assign",
    status_code=status.HTTP_200_OK,
    summary="Release a job I assigned to myself",
    responses={403: {"description": "Assigned to someone else"}, 404: {"description": "Job not found"}},
)
async def unassign_job_route(
    job_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    job = await unassign_job(db, job_id, current_user)
    logger.info("job_unassigned", job_id=job_id, user=current_user.email)
    return {"job_id": job_id, "assigned_to": assignment_view(job)}
