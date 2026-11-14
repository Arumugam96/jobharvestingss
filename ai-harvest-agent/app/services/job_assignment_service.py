"""Self-assignment of harvested jobs ("Assign to me" on the Harvested Jobs table).

A user claims a job for themselves, optionally with a note. Once claimed, the job
is locked to that user: everyone can still see the row (and who owns it) in
GET /jobs, but only the assignee can open its detail or run outreach on it —
the routes call ensure_job_access() before doing either. The assignee can
release the job again; nobody else can.

Identity is the user's email (users have no separate display name); the shown
name is derived from it at claim time. Matching is case-insensitive.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_context import apply_tenant
from app.models.auth import AuthenticatedUser
from app.models.harvest_run import ScrapedJobORM

NOTE_MAX_LEN = 1000


def display_name_from_email(email: str) -> str:
    """"Get's the name of the user using their email"."""
    local = (email or "").split("@", 1)[0]
    words = [w for w in local.replace("_", ".").replace("-", ".").split(".") if w]
    return " ".join(w.capitalize() for w in words) or email


def assignment_view(job: ScrapedJobORM) -> dict[str, Any] | None:
    """The `assigned_to` block of a job's API view; None when unassigned."""
    email = getattr(job, "assigned_to_email", None)
    if not email:
        return None
    assigned_at = getattr(job, "assigned_at", None)
    return {
        "email": email,
        "name": getattr(job, "assigned_to_name", "") or display_name_from_email(email),
        "assigned_at": assigned_at.isoformat() if assigned_at else None,
        "note": getattr(job, "assignment_note", "") or "",
    }


def _same_user(email: str | None, user: AuthenticatedUser) -> bool:
    """Case-insensitive match of an assignee email against the user; False if email is empty."""
    return bool(email) and email.lower() == (user.email or "").lower()


def ensure_job_access(assigned_to: dict[str, Any] | str | None, user: AuthenticatedUser) -> None:
    """Raise 403 when the job is assigned to someone other than `user`.

    Accepts either an assignment_view() block or the raw assigned_to_email."""
    if not assigned_to:
        return
    email = assigned_to.get("email") if isinstance(assigned_to, dict) else assigned_to
    if _same_user(email, user):
        return
    name = assigned_to.get("name") if isinstance(assigned_to, dict) else display_name_from_email(email)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"This job is assigned to {name}. Only they can open or work on it.",
    )


async def assign_job(db: AsyncSession, job_id: str, user: AuthenticatedUser, note: str = "") -> ScrapedJobORM:
    """Claim `job_id` for `user` (or update the note if they already own it).

    A single conditional UPDATE, so two users clicking at once can't both win:
    it only matches while the job is unassigned or already the caller's."""
    note = (note or "").strip()[:NOTE_MAX_LEN]
    stmt = apply_tenant(
        update(ScrapedJobORM)
        .where(
            ScrapedJobORM.id == job_id,
            or_(
                ScrapedJobORM.assigned_to_email.is_(None),
                func.lower(ScrapedJobORM.assigned_to_email) == (user.email or "").lower(),
            ),
        )
        .values(
            assigned_to_email=user.email,
            assigned_to_name=display_name_from_email(user.email),
            assigned_at=datetime.now(timezone.utc),
            assignment_note=note,
        ),
        ScrapedJobORM.tenant_id,
    )
    result = await db.execute(stmt)

    row = await db.execute(select(ScrapedJobORM).where(ScrapedJobORM.id == job_id))
    job = row.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    if result.rowcount == 0:
        await db.refresh(job)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Already assigned to {job.assigned_to_name or display_name_from_email(job.assigned_to_email)}.",
        )
    await db.commit()
    await db.refresh(job)
    return job


async def unassign_job(db: AsyncSession, job_id: str, user: AuthenticatedUser) -> ScrapedJobORM:
    """Release `job_id`. Only the current assignee may do this."""
    row = await db.execute(select(ScrapedJobORM).where(ScrapedJobORM.id == job_id))
    job = row.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    if not job.assigned_to_email:
        return job
    if not _same_user(job.assigned_to_email, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Only {job.assigned_to_name or job.assigned_to_email} can release this job.",
        )
    job.assigned_to_email = None
    job.assigned_to_name = ""
    job.assigned_at = None
    job.assignment_note = ""
    await db.commit()
    await db.refresh(job)
    return job
