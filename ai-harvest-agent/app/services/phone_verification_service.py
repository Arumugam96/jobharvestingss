"""Phone call verification ("Verify mobile number" on the Harvested Jobs table).

A user dials a job's phone number and logs the result: Answered (remarks
required) or Unanswered, optionally with Follow-up on. Every log is an
append-only phone_call_logs row (app/models/phone_call_log.py):

  * The newest non-cleared row per job is the badge the whole tenant team sees
    (latest_for_jobs), including who called.
  * An Unanswered call with Follow-up on gets follow_up_due_at = now +
    settings.phone_followup_delay_hours. Once that passes it shows in the
    CALLER's notification bell (due_followups) — computed at poll time, so no
    scheduler job is involved. A newer call on the same job+phone, the caller
    dismissing it, or "Clear status" closes it (follow_up_done_at).
  * "Clear status" soft-deletes (cleared_at) the job+phone's rows.

job_id is a soft link to scraped_jobs.id; a run reconcile that rewrites a run's
rows re-points it via relink_job_ids(). Matching on the caller is
case-insensitive on email, like job_assignment_service.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.tenant_context import apply_tenant, get_current_tenant_id
from app.models.auth import AuthenticatedUser
from app.models.harvest_run import ScrapedJobORM
from app.models.phone_call_log import PhoneCallLogORM
from app.services.job_assignment_service import display_name_from_email

CALL_STATUSES = ("answered", "unanswered")
REMARKS_MAX_LEN = 2000
PHONE_MAX_LEN = 50
PHONE_SOURCE_MAX_LEN = 20
CONTACT_NAME_MAX_LEN = 255


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite (tests) hands back naive datetimes; everything stored is UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    dt = _aware(dt)
    return dt.isoformat() if dt else None


def _same_user(email: str | None, user: AuthenticatedUser) -> bool:
    return bool(email) and email.lower() == (user.email or "").lower()


def call_view(row: PhoneCallLogORM) -> dict[str, Any]:
    """API view of one logged call."""
    return {
        "id": row.id,
        "job_id": row.job_id,
        "phone": row.phone,
        "phone_source": row.phone_source or "",
        "contact_name": row.contact_name or "",
        "status": row.call_status,
        "remarks": row.remarks or "",
        "follow_up": bool(row.follow_up),
        "follow_up_due_at": _iso(row.follow_up_due_at),
        "follow_up_done_at": _iso(row.follow_up_done_at),
        "called_by": {
            "email": row.called_by_email,
            "name": row.called_by_name or display_name_from_email(row.called_by_email),
        },
        "called_at": _iso(row.created_at),
    }


def _close_open_followups_stmt(job_id: str, phone: str, now: datetime):
    return apply_tenant(
        update(PhoneCallLogORM)
        .where(
            PhoneCallLogORM.job_id == job_id,
            PhoneCallLogORM.phone == phone,
            PhoneCallLogORM.follow_up_due_at.is_not(None),
            PhoneCallLogORM.follow_up_done_at.is_(None),
        )
        .values(follow_up_done_at=now, updated_at=now),
        PhoneCallLogORM.tenant_id,
    )


async def log_call(
    db: AsyncSession,
    job: ScrapedJobORM,
    user: AuthenticatedUser,
    *,
    phone: str,
    phone_source: str = "",
    contact_name: str = "",
    status: str,
    remarks: str = "",
    follow_up: bool = False,
) -> PhoneCallLogORM:
    """Record one call against `job`'s `phone`. Supersedes (closes) any open
    follow-up on the same job+phone, then inserts the new row."""
    call_status = (status or "").strip().lower()
    if call_status not in CALL_STATUSES:
        raise HTTPException(
            status_code=422, detail=f"status must be one of {', '.join(CALL_STATUSES)}"
        )
    phone = (phone or "").strip()[:PHONE_MAX_LEN]
    if not phone:
        raise HTTPException(status_code=422, detail="phone is required")
    remarks = (remarks or "").strip()[:REMARKS_MAX_LEN]
    if call_status == "answered" and not remarks:
        raise HTTPException(status_code=422, detail="Remarks are required for an answered call")

    now = _now()
    await db.execute(_close_open_followups_stmt(job.id, phone, now))

    due = None
    if call_status == "unanswered" and follow_up:
        due = now + timedelta(hours=get_settings().phone_followup_delay_hours)

    row = PhoneCallLogORM(
        # The job's tenant, not the caller's: an internal (all-access) user
        # logging a call on a client job must not hide it from that client.
        tenant_id=getattr(job, "tenant_id", None) or get_current_tenant_id(),
        job_id=job.id,
        phone=phone,
        phone_source=(phone_source or "").strip()[:PHONE_SOURCE_MAX_LEN],
        contact_name=(contact_name or "").strip()[:CONTACT_NAME_MAX_LEN],
        call_status=call_status,
        remarks=remarks,
        follow_up=bool(follow_up),
        follow_up_due_at=due,
        called_by_email=user.email or "",
        called_by_name=display_name_from_email(user.email or ""),
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def clear_calls(db: AsyncSession, job_id: str, phone: str | None = None) -> int:
    """"Clear status": soft-delete the job's active rows for `phone` (all phones
    when blank) and close their open follow-ups. Returns rows cleared."""
    now = _now()
    conds = [PhoneCallLogORM.job_id == job_id, PhoneCallLogORM.cleared_at.is_(None)]
    phone = (phone or "").strip()
    if phone:
        conds.append(PhoneCallLogORM.phone == phone)

    await db.execute(apply_tenant(
        update(PhoneCallLogORM)
        .where(*conds, PhoneCallLogORM.follow_up_done_at.is_(None),
               PhoneCallLogORM.follow_up_due_at.is_not(None))
        .values(follow_up_done_at=now),
        PhoneCallLogORM.tenant_id,
    ))
    result = await db.execute(apply_tenant(
        update(PhoneCallLogORM).where(*conds).values(cleared_at=now, updated_at=now),
        PhoneCallLogORM.tenant_id,
    ))
    await db.commit()
    return result.rowcount or 0


async def latest_for_jobs(db: AsyncSession, job_ids: list[str]) -> dict[str, dict[str, Any]]:
    """{job_id: call_view} — the newest non-cleared call per job (tenant-scoped).
    Ids are autoincrement, so max(id) per job is the newest row."""
    ids = [j for j in dict.fromkeys(job_ids) if j]
    if not ids:
        return {}
    newest = apply_tenant(
        select(func.max(PhoneCallLogORM.id))
        .where(PhoneCallLogORM.job_id.in_(ids), PhoneCallLogORM.cleared_at.is_(None))
        .group_by(PhoneCallLogORM.job_id),
        PhoneCallLogORM.tenant_id,
    )
    rows = (await db.execute(
        select(PhoneCallLogORM).where(PhoneCallLogORM.id.in_(newest.scalar_subquery()))
    )).scalars().all()
    return {r.job_id: call_view(r) for r in rows}


async def history(db: AsyncSession, job_id: str) -> list[dict[str, Any]]:
    """All non-cleared calls on a job, newest first (tenant-scoped)."""
    stmt = apply_tenant(
        select(PhoneCallLogORM)
        .where(PhoneCallLogORM.job_id == job_id, PhoneCallLogORM.cleared_at.is_(None))
        .order_by(PhoneCallLogORM.created_at.desc(), PhoneCallLogORM.id.desc()),
        PhoneCallLogORM.tenant_id,
    )
    return [call_view(r) for r in (await db.execute(stmt)).scalars().all()]


async def relink_job_ids(db: AsyncSession, mapping: dict[str, str]) -> None:
    """Re-point call logs from old scraped_jobs ids to new ones after a run
    reconcile rewrote the run's rows. Does NOT commit — the caller owns the
    transaction. Not tenant-filtered: job ids are uuids, and the reconcile may
    run outside a request's tenant context."""
    for old_id, new_id in (mapping or {}).items():
        if not old_id or not new_id or old_id == new_id:
            continue
        await db.execute(
            update(PhoneCallLogORM)
            .where(PhoneCallLogORM.job_id == old_id)
            .values(job_id=new_id)
        )


def _due_conditions(user: AuthenticatedUser, now: datetime) -> list:
    return [
        func.lower(PhoneCallLogORM.called_by_email) == (user.email or "").lower(),
        PhoneCallLogORM.follow_up_due_at.is_not(None),
        PhoneCallLogORM.follow_up_due_at <= now,
        PhoneCallLogORM.follow_up_done_at.is_(None),
        PhoneCallLogORM.cleared_at.is_(None),
    ]


async def due_followups(
    db: AsyncSession, user: AuthenticatedUser, limit: int = 20
) -> tuple[int, list[dict[str, Any]]]:
    """(count, items) of `user`'s follow-up calls that are now due, newest due
    first. Each item is a call_view plus type/company/job_title (the job may
    have been deleted, hence the outer join)."""
    now = _now()
    conds = _due_conditions(user, now)
    count = (await db.execute(apply_tenant(
        select(func.count(PhoneCallLogORM.id)).where(*conds), PhoneCallLogORM.tenant_id,
    ))).scalar_one()

    stmt = apply_tenant(
        select(PhoneCallLogORM, ScrapedJobORM.company, ScrapedJobORM.job_title)
        .outerjoin(ScrapedJobORM, ScrapedJobORM.id == PhoneCallLogORM.job_id)
        .where(*conds)
        .order_by(PhoneCallLogORM.follow_up_due_at.desc(), PhoneCallLogORM.id.desc())
        .limit(max(1, int(limit or 20))),
        PhoneCallLogORM.tenant_id,
    )
    items = []
    for row, company, job_title in (await db.execute(stmt)).all():
        view = call_view(row)
        view.update(
            type="call_followup",
            contact_name=row.contact_name or "",
            company=company or "",
            job_title=job_title or "",
        )
        items.append(view)
    return int(count or 0), items


async def dismiss_followup(
    db: AsyncSession, log_id: int, user: AuthenticatedUser
) -> PhoneCallLogORM:
    """Mark one follow-up done. 404 if missing, 403 if `user` isn't the caller."""
    row = (await db.execute(apply_tenant(
        select(PhoneCallLogORM).where(PhoneCallLogORM.id == log_id), PhoneCallLogORM.tenant_id,
    ))).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail=f"Call log {log_id} not found"
        )
    if not _same_user(row.called_by_email, user):
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Only the person who logged this call can dismiss its follow-up.",
        )
    if row.follow_up_done_at is None:
        now = _now()
        row.follow_up_done_at = now
        row.updated_at = now
        await db.commit()
        await db.refresh(row)
    return row


async def dismiss_all(db: AsyncSession, user: AuthenticatedUser) -> int:
    """Mark every currently-due follow-up of `user` done. Returns rows updated."""
    now = _now()
    result = await db.execute(apply_tenant(
        update(PhoneCallLogORM)
        .where(*_due_conditions(user, now))
        .values(follow_up_done_at=now, updated_at=now),
        PhoneCallLogORM.tenant_id,
    ))
    await db.commit()
    return result.rowcount or 0
