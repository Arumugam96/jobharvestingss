"""
Job comment routes (My Workspace "Comments" column) — see job_comment_service.

Endpoints
─────────
GET    /jobs/comments/latest?job_ids=a,b     — newest comment + count per job (table)
GET    /jobs/{id}/comments                   — the job's comments, newest first, + assignment_note
POST   /jobs/{id}/comments                   — add a comment {body}
PATCH  /jobs/{id}/comments/{comment_id}      — edit (author or admin)
DELETE /jobs/{id}/comments/{comment_id}      — delete (author or admin)

All honour the self-assignment lock (403 when the job is assigned to someone else;
admins pass). Served under /jobs/, which nginx already proxies for fetch() calls.
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user, get_db_session
from app.models.auth import AuthenticatedUser
from app.services import job_comment_service as jcs

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Jobs & Leads"])


class CommentRequest(BaseModel):
    body: str = Field(..., max_length=jcs.COMMENT_MAX_LEN, description="Comment text.")


# Declared BEFORE /jobs/{job_id}/comments so "comments" is never taken as a job id.
@router.get("/jobs/comments/latest", status_code=status.HTTP_200_OK,
            summary="Newest comment per job (My Workspace table)")
async def latest_job_comments(
    job_ids: str = Query("", description="Comma-separated job ids (max 200)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """`{"items": {job_id: {"latest": comment, "count": n}}}` — jobs without comments are absent."""
    ids = [j.strip() for j in job_ids.split(",") if j.strip()]
    return {"items": await jcs.latest_for_jobs(db, ids, current_user)}


@router.get("/jobs/{job_id}/comments", status_code=status.HTTP_200_OK,
            summary="List a job's comments",
            responses={403: {"description": "Assigned to someone else"}, 404: {"description": "Job not found"}})
async def list_job_comments(
    job_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    return await jcs.list_comments(db, job_id, current_user)


@router.post("/jobs/{job_id}/comments", status_code=status.HTTP_201_CREATED,
             summary="Add a comment to a job",
             responses={403: {"description": "Assigned to someone else"}, 404: {"description": "Job not found"},
                        422: {"description": "Empty or too long"}})
async def add_job_comment(
    job_id: str,
    body: CommentRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    item = await jcs.add_comment(db, job_id, current_user, body.body)
    logger.info("job_comment_added", job_id=job_id, user=current_user.email)
    return item


@router.patch("/jobs/{job_id}/comments/{comment_id}", status_code=status.HTTP_200_OK,
              summary="Edit a job comment (author or admin)",
              responses={403: {"description": "Not your comment / job locked"},
                         404: {"description": "Not found"}, 422: {"description": "Empty or too long"}})
async def edit_job_comment(
    job_id: str,
    comment_id: int,
    body: CommentRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    return await jcs.update_comment(db, job_id, comment_id, current_user, body.body)


@router.delete("/jobs/{job_id}/comments/{comment_id}", status_code=status.HTTP_200_OK,
               summary="Delete a job comment (author or admin)",
               responses={403: {"description": "Not your comment / job locked"},
                          404: {"description": "Not found"}})
async def delete_job_comment(
    job_id: str,
    comment_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    await jcs.delete_comment(db, job_id, comment_id, current_user)
    return {"job_id": job_id, "deleted": comment_id}
