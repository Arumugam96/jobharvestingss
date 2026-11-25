"""Job comments (My Workspace "Comments" column) — see app/models/job_comment.py.

Access follows the job's self-assignment lock (ensure_job_access): the assignee,
anyone on an unassigned job, and admins may read and add comments. Only a
comment's author — or an admin — may edit or delete it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_context import apply_tenant
from app.models.auth import AuthenticatedUser
from app.models.harvest_run import ScrapedJobORM
from app.models.job_comment import JobCommentORM
from app.services.job_assignment_service import display_name_from_email, ensure_job_access

COMMENT_MAX_LEN = 4000
LATEST_MAX_JOBS = 200


def _same_user(email: str | None, user: AuthenticatedUser) -> bool:
    return bool(email) and email.lower() == (user.email or "").lower()


def can_edit_comment(row: JobCommentORM, user: AuthenticatedUser) -> bool:
    """Admins: any comment. Everyone else: only their own."""
    return user.is_admin or _same_user(row.author_email, user)


def comment_view(row: JobCommentORM, user: AuthenticatedUser) -> dict[str, Any]:
    """API shape of one comment."""
    return {
        "id": row.id,
        "job_id": row.job_id,
        "body": row.body or "",
        "author_email": row.author_email or "",
        "author_name": row.author_name or display_name_from_email(row.author_email or ""),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "edited": bool(row.created_at and row.updated_at
                       and (row.updated_at - row.created_at).total_seconds() > 1),
        "can_edit": can_edit_comment(row, user),
    }


def _clean_body(body: str) -> str:
    text = (body or "").strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Comment cannot be empty.")
    if len(text) > COMMENT_MAX_LEN:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"Comment is longer than {COMMENT_MAX_LEN} characters.")
    return text


async def load_job_guarded(db: AsyncSession, job_id: str, user: AuthenticatedUser) -> ScrapedJobORM:
    """The job (current tenant): 404 if missing, 403 if locked to someone else (admins pass)."""
    stmt = apply_tenant(select(ScrapedJobORM).where(ScrapedJobORM.id == job_id), ScrapedJobORM.tenant_id)
    job = (await db.execute(stmt)).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    ensure_job_access(job.assigned_to_email, user)
    return job


async def list_comments(db: AsyncSession, job_id: str, user: AuthenticatedUser) -> dict[str, Any]:
    """The job's comments, newest first, plus its assignment note (the fallback comment)."""
    job = await load_job_guarded(db, job_id, user)
    stmt = apply_tenant(
        select(JobCommentORM).where(JobCommentORM.job_id == job_id), JobCommentORM.tenant_id,
    ).order_by(JobCommentORM.created_at.desc(), JobCommentORM.id.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return {
        "job_id": job_id,
        "comments": [comment_view(r, user) for r in rows],
        "assignment_note": job.assignment_note or "",
    }


async def add_comment(db: AsyncSession, job_id: str, user: AuthenticatedUser, body: str) -> dict[str, Any]:
    job = await load_job_guarded(db, job_id, user)
    row = JobCommentORM(
        tenant_id=job.tenant_id,
        job_id=job_id,
        body=_clean_body(body),
        author_email=user.email or "",
        author_name=display_name_from_email(user.email or ""),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return comment_view(row, user)


async def _load_comment(db: AsyncSession, job_id: str, comment_id: int,
                        user: AuthenticatedUser) -> JobCommentORM:
    await load_job_guarded(db, job_id, user)
    stmt = apply_tenant(
        select(JobCommentORM).where(JobCommentORM.id == comment_id, JobCommentORM.job_id == job_id),
        JobCommentORM.tenant_id,
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")
    if not can_edit_comment(row, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Only the comment's author or an admin can change it.")
    return row


async def update_comment(db: AsyncSession, job_id: str, comment_id: int,
                         user: AuthenticatedUser, body: str) -> dict[str, Any]:
    row = await _load_comment(db, job_id, comment_id, user)
    row.body = _clean_body(body)
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    return comment_view(row, user)


async def delete_comment(db: AsyncSession, job_id: str, comment_id: int,
                         user: AuthenticatedUser) -> None:
    row = await _load_comment(db, job_id, comment_id, user)
    await db.delete(row)
    await db.commit()


async def latest_for_jobs(db: AsyncSession, job_ids: list[str],
                          user: AuthenticatedUser) -> dict[str, dict[str, Any]]:
    """{job_id: {"latest": comment_view | None, "count": n}} for the given jobs (current
    tenant). Jobs locked to someone else are omitted for non-admins, mirroring
    GET /phone-verifications/latest."""
    ids = [j for j in dict.fromkeys(job_ids) if j][:LATEST_MAX_JOBS]
    if not ids:
        return {}
    jobs_stmt = apply_tenant(
        select(ScrapedJobORM.id, ScrapedJobORM.assigned_to_email).where(ScrapedJobORM.id.in_(ids)),
        ScrapedJobORM.tenant_id,
    )
    visible = [
        jid for jid, assignee in (await db.execute(jobs_stmt)).all()
        if user.is_admin or not assignee or _same_user(assignee, user)
    ]
    if not visible:
        return {}
    counts_stmt = apply_tenant(
        select(JobCommentORM.job_id, func.count(JobCommentORM.id))
        .where(JobCommentORM.job_id.in_(visible)).group_by(JobCommentORM.job_id),
        JobCommentORM.tenant_id,
    )
    counts = dict((await db.execute(counts_stmt)).all())
    if not counts:
        return {}
    rows_stmt = apply_tenant(
        select(JobCommentORM).where(JobCommentORM.job_id.in_(list(counts))),
        JobCommentORM.tenant_id,
    ).order_by(JobCommentORM.created_at.desc(), JobCommentORM.id.desc())
    out: dict[str, dict[str, Any]] = {}
    for row in (await db.execute(rows_stmt)).scalars():
        if row.job_id not in out:
            out[row.job_id] = {"latest": comment_view(row, user), "count": counts[row.job_id]}
    return out
