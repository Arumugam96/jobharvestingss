"""Automatic round-robin assignment of newly harvested jobs to Business Leads.

Every harvest write (HarvestRunService.bulk_insert_scraped_jobs, and the
end-of-run replace_run_jobs reconcile) hands its fresh rows to
auto_assign_jobs(). A job is eligible when it is still unassigned, is not a
Demand Search import, and has at least one email or phone — the same "has
contact" test the jobs table uses (_contact_presence_exprs). Eligible jobs are
dealt out in job order to the tenant's active Business Leads (users.role =
business_lead), sorted by lower(email), continuing from where the previous
harvest stopped.

Sticky contact owner: before the rotation, a job whose contact (company + shared
phone / recruiter / poster email / LinkedIn — phone_verification_service's
ContactIdentity) already has an inheritable call on another job goes to that call's
owner (the call's assigned_to_user_id, else its caller) when that owner is still one
of the tenant's active Business Leads — assignment_source "contact", no rotation
slot spent. Otherwise the job falls through to the round-robin. The owners of a
whole batch are resolved in bulk (phone_verification_service.contact_call_owners).

The rotation cursor lives in job_assignment_rotation (one row per tenant) and
stores the LAST Business Lead assigned, not a counter, so adding or removing a
Business Lead never skews the rotation. The row is locked with SELECT ... FOR
UPDATE for the rest of the caller's transaction, so concurrent harvest writers
serialise; every UPDATE is additionally conditional on the job still being
unassigned, so a manual "Assign to me" (or a parallel writer) always wins and a
job is never assigned twice. A lost race doesn't consume a rotation slot.

Demand Search jobs (source "Demand" — uploaded client requirements, not
harvested) never enter the rotation. Instead assign_jobs_to_searcher() gives a
Demand batch's jobs to the user who ran the search, but only when that user is
an active Business Lead of the jobs' tenant — regardless of contact presence
(it's their own search) and without touching the rotation cursor.

Auto-assignment is best-effort: it runs inside a SAVEPOINT and any failure is
logged and rolled back to it, so the harvest write itself always commits.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

import structlog
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.auth import BUSINESS_LEAD_ROLE, UserORM
from app.models.harvest_run import ScrapedJobORM
from app.models.job_assignment_rotation import JobAssignmentRotationORM
from app.models.recruiter import RecruiterORM
from app.services.harvest_run_service import (
    DEMAND_SOURCE,
    _contact_presence_exprs,
    _not_demand_job,
)
from app.services.job_assignment_service import display_name_from_email
from app.services.phone_verification_service import contact_call_owners

logger = structlog.get_logger(__name__)

AUTO_ASSIGN_NOTE = "Auto-assigned (round-robin)"
CONTACT_ASSIGN_NOTE = "Auto-assigned (contact owner)"
SEARCHER_ASSIGN_NOTE = "Auto-assigned (Demand search owner)"
# scraped_jobs.assignment_source values ("" = manual claim).
SOURCE_AUTO = "auto"
SOURCE_CONTACT = "contact"  # sticky owner of an already-called contact
SOURCE_DEMAND = "demand"

# Keep IN (...) lists comfortably under every driver's bind-parameter limit.
_ID_CHUNK = 500


def _unassigned():
    """SQL predicate: the job has no assignee (NULL or empty string)."""
    return or_(ScrapedJobORM.assigned_to_email.is_(None), ScrapedJobORM.assigned_to_email == "")


def next_business_lead(leads: list[str], last_email: str | None) -> str:
    """The Business Lead after ``last_email`` in ``leads`` (sorted, lower-cased),
    wrapping to the first. ``last_email`` need not be in the list any more — the
    first lead sorting after it is picked, so removals/additions stay fair."""
    last = (last_email or "").lower()
    if last:
        for email in leads:
            if email.lower() > last:
                return email
    return leads[0]


async def active_business_leads(db: AsyncSession, tenant_id: str) -> list[str]:
    """Emails of the tenant's active Business Leads, ordered by lower(email).

    Sorted in Python (not by the DB collation) so the order always agrees with
    next_business_lead()'s plain string comparison."""
    result = await db.execute(
        select(UserORM.email)
        .where(
            UserORM.role == BUSINESS_LEAD_ROLE,
            UserORM.is_active.is_(True),
            UserORM.tenant_id == tenant_id,
            func.coalesce(UserORM.email, "") != "",
        )
    )
    seen: set[str] = set()
    leads: list[str] = []
    for (email,) in result.all():
        key = email.strip().lower()
        if key and key not in seen:
            seen.add(key)
            leads.append(email.strip())
    return sorted(leads, key=str.lower)


async def _lock_rotation(db: AsyncSession, tenant_id: str) -> JobAssignmentRotationORM:
    """Insert the tenant's rotation row if missing, then lock it FOR UPDATE until
    the surrounding transaction ends (FOR UPDATE is a no-op on SQLite, which
    serialises writers on its own)."""
    dialect = db.get_bind().dialect.name
    values = {"tenant_id": tenant_id, "last_assigned_email": ""}
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:  # pragma: no cover — only Postgres (prod) and SQLite (tests) are used
        dialect_insert = None
    if dialect_insert is not None:
        await db.execute(
            dialect_insert(JobAssignmentRotationORM)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["tenant_id"])
        )
    else:  # pragma: no cover
        exists = await db.get(JobAssignmentRotationORM, tenant_id)
        if exists is None:
            db.add(JobAssignmentRotationORM(**values))
            await db.flush()
    result = await db.execute(
        select(JobAssignmentRotationORM)
        .where(JobAssignmentRotationORM.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


def _eligible_stmt():
    """Base SELECT (id, tenant_id) of unassigned, non-Demand jobs with ≥1 email or phone."""
    presence = _contact_presence_exprs()
    return (
        select(ScrapedJobORM.id, ScrapedJobORM.tenant_id)
        .select_from(ScrapedJobORM)
        .outerjoin(RecruiterORM, ScrapedJobORM.recruiter_id == RecruiterORM.id)
        .where(_unassigned(), _not_demand_job(), or_(presence["email"], presence["mobile"]))
    )


async def _eligible_jobs(
    db: AsyncSession, job_ids: list[str] | None, run_pk: str | None
) -> list[tuple[str, str]]:
    """[(job id, tenant id)] in job order. Explicit ``job_ids`` keep the caller's
    (insertion) order; a ``run_pk`` scan orders by scraped_at, created_at, id."""
    if job_ids is not None:
        found: dict[str, str] = {}
        for i in range(0, len(job_ids), _ID_CHUNK):
            chunk = job_ids[i:i + _ID_CHUNK]
            rows = await db.execute(_eligible_stmt().where(ScrapedJobORM.id.in_(chunk)))
            found.update({jid: tid for jid, tid in rows.all()})
        return [(jid, found[jid]) for jid in job_ids if jid in found]
    rows = await db.execute(
        _eligible_stmt()
        .where(ScrapedJobORM.run_id == run_pk)
        .order_by(ScrapedJobORM.scraped_at, ScrapedJobORM.created_at, ScrapedJobORM.id)
    )
    return [(jid, tid) for jid, tid in rows.all()]


async def _assign_contact_owners(
    db: AsyncSession, tenant_id: str, job_ids: list[str], leads: list[str], now: datetime,
) -> tuple[set[str], int]:
    """Give each job whose contact was already called to that call's owner when the
    owner is one of ``leads`` (the tenant's active Business Leads). Returns (the ids
    handled here — assigned, or lost to a concurrent claim — so the round-robin skips
    them, how many were assigned); jobs whose owner isn't an active lead are not
    handled."""
    lead_by_key = {lead.lower(): lead for lead in leads}
    by_lead: dict[str, list[str]] = {}
    for job_id, owner in (await contact_call_owners(db, tenant_id, job_ids)).items():
        lead = lead_by_key.get(owner.strip().lower())
        if lead:
            by_lead.setdefault(lead, []).append(job_id)
    handled: set[str] = set()
    total = 0
    for lead, ids in sorted(by_lead.items()):
        assigned = 0
        for i in range(0, len(ids), _ID_CHUNK):
            result = await db.execute(
                update(ScrapedJobORM)
                .where(ScrapedJobORM.id.in_(ids[i:i + _ID_CHUNK]), _unassigned())
                .values(
                    assigned_to_email=lead,
                    assigned_to_name=display_name_from_email(lead),
                    assigned_at=now,
                    assignment_note=CONTACT_ASSIGN_NOTE,
                    assignment_source=SOURCE_CONTACT,
                    assignment_reminder_done_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            assigned += result.rowcount or 0
        handled.update(ids)
        total += assigned
        logger.info("job_contact_owner_assigned", tenant_id=tenant_id, lead=lead,
                    assigned=assigned, matched=len(ids))
    return handled, total


async def _assign_tenant(db: AsyncSession, tenant_id: str, job_ids: list[str]) -> int:
    """Give ``job_ids`` (one tenant, job order) to its Business Leads: first each
    called contact's owner (_assign_contact_owners), then the rest round-robin.
    Returns how many jobs were assigned."""
    leads = await active_business_leads(db, tenant_id)
    if not leads:
        logger.info("job_auto_assign_skipped", reason="no_business_leads",
                    tenant_id=tenant_id, eligible=len(job_ids))
        return 0
    sticky, sticky_assigned = await _assign_contact_owners(
        db, tenant_id, job_ids, leads, datetime.now(timezone.utc))
    job_ids = [j for j in job_ids if j not in sticky]
    if not job_ids:
        return sticky_assigned
    rotation = await _lock_rotation(db, tenant_id)
    last = rotation.last_assigned_email or ""
    now = datetime.now(timezone.utc)
    assigned = 0
    per_lead: dict[str, int] = {}
    for job_id in job_ids:
        lead = next_business_lead(leads, last)
        result = await db.execute(
            update(ScrapedJobORM)
            .where(ScrapedJobORM.id == job_id, _unassigned())
            .values(
                assigned_to_email=lead,
                assigned_to_name=display_name_from_email(lead),
                assigned_at=now,
                assignment_note=AUTO_ASSIGN_NOTE,
                assignment_source=SOURCE_AUTO,
                assignment_reminder_done_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount:  # lost the race to a manual claim → slot not consumed
            last = lead.lower()
            assigned += 1
            per_lead[lead] = per_lead.get(lead, 0) + 1
    if assigned:
        rotation.last_assigned_email = last
        rotation.updated_at = now
        await db.flush()
    logger.info("job_auto_assigned", tenant_id=tenant_id, assigned=assigned,
                eligible=len(job_ids), per_lead=per_lead, last_assigned=last)
    return sticky_assigned + assigned


async def _auto_assign(
    db: AsyncSession, job_ids: list[str] | None, run_pk: str | None
) -> int:
    """Unguarded core of auto_assign_jobs (no settings check, no savepoint)."""
    eligible = await _eligible_jobs(db, job_ids, run_pk)
    if not eligible:
        logger.debug("job_auto_assign_skipped", reason="no_eligible_jobs", run_pk=run_pk)
        return 0
    by_tenant: dict[str, list[str]] = {}
    for jid, tid in eligible:
        by_tenant.setdefault(tid or "", []).append(jid)
    total = 0
    for tenant_id in sorted(by_tenant):  # fixed lock order → no cross-tenant deadlock
        total += await _assign_tenant(db, tenant_id, by_tenant[tenant_id])
    return total


async def auto_assign_jobs(
    db: AsyncSession,
    *,
    job_ids: Iterable[str] | None = None,
    run_pk: str | None = None,
) -> int:
    """Auto-assign eligible jobs round-robin to Business Leads; returns the count.

    Pass ``job_ids`` (rows just inserted, in job order) or ``run_pk`` (every
    still-unassigned row of that run). Runs in a SAVEPOINT inside the caller's
    transaction and never raises: a failure is logged, rolled back to the
    savepoint and reported as 0, so the harvest write still commits. No-op when
    settings.job_auto_assign_enabled is off."""
    if not get_settings().job_auto_assign_enabled:
        return 0
    ids: list[str] | None = list(job_ids) if job_ids is not None else None
    if ids is not None and not ids:
        return 0
    if ids is None and not run_pk:
        return 0
    try:
        async with db.begin_nested():
            return await _auto_assign(db, ids, run_pk)
    except Exception as exc:
        logger.warning("job_auto_assign_failed", error=str(exc), run_pk=run_pk)
        return 0


async def _business_lead_email(db: AsyncSession, email: str, tenant_id: str) -> str | None:
    """The stored email of ``email``'s user when they are an active Business Lead
    of ``tenant_id`` (case-insensitive match); None otherwise."""
    result = await db.execute(
        select(UserORM.email).where(
            func.lower(UserORM.email) == email.strip().lower(),
            UserORM.role == BUSINESS_LEAD_ROLE,
            UserORM.is_active.is_(True),
            UserORM.tenant_id == tenant_id,
        )
    )
    found = result.scalars().first()
    return found.strip() if found else None


async def _assign_to_searcher(db: AsyncSession, job_ids: list[str], searcher_email: str) -> int:
    """Unguarded core of assign_jobs_to_searcher (no settings check, no savepoint)."""
    by_tenant: dict[str, list[str]] = {}
    for i in range(0, len(job_ids), _ID_CHUNK):
        rows = await db.execute(
            select(ScrapedJobORM.id, ScrapedJobORM.tenant_id).where(
                ScrapedJobORM.id.in_(job_ids[i:i + _ID_CHUNK]),
                _unassigned(),
                func.lower(ScrapedJobORM.source) == DEMAND_SOURCE.lower(),
            )
        )
        for jid, tid in rows.all():
            by_tenant.setdefault(tid or "", []).append(jid)
    now = datetime.now(timezone.utc)
    total = 0
    for tenant_id, ids in sorted(by_tenant.items()):
        lead = await _business_lead_email(db, searcher_email, tenant_id)
        if lead is None:
            logger.info("demand_auto_assign_skipped", reason="searcher_not_business_lead",
                        tenant_id=tenant_id, searcher=searcher_email, jobs=len(ids))
            continue
        assigned = 0
        for i in range(0, len(ids), _ID_CHUNK):
            result = await db.execute(
                update(ScrapedJobORM)
                .where(ScrapedJobORM.id.in_(ids[i:i + _ID_CHUNK]), _unassigned())
                .values(
                    assigned_to_email=lead,
                    assigned_to_name=display_name_from_email(lead),
                    assigned_at=now,
                    assignment_note=SEARCHER_ASSIGN_NOTE,
                    assignment_source=SOURCE_DEMAND,
                    assignment_reminder_done_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            assigned += result.rowcount or 0
        total += assigned
        logger.info("demand_auto_assigned", tenant_id=tenant_id, lead=lead, assigned=assigned)
    return total


async def assign_jobs_to_searcher(
    db: AsyncSession, *, job_ids: Iterable[str], searcher_email: str | None
) -> int:
    """Assign Demand Search jobs to the user who ran the search; returns the count.

    Only still-unassigned Demand rows among ``job_ids`` are touched, and only when
    ``searcher_email`` belongs to an active Business Lead of the job's tenant
    (otherwise the jobs stay unassigned). The rotation cursor is never read or
    advanced. Same safety net as auto_assign_jobs: SAVEPOINT, never raises, no-op
    when settings.job_auto_assign_enabled is off."""
    if not get_settings().job_auto_assign_enabled:
        return 0
    ids = list(job_ids)
    email = (searcher_email or "").strip()
    if not ids or not email:
        if ids:
            logger.info("demand_auto_assign_skipped", reason="no_searcher", jobs=len(ids))
        return 0
    try:
        async with db.begin_nested():
            return await _assign_to_searcher(db, ids, email)
    except Exception as exc:
        logger.warning("demand_auto_assign_failed", error=str(exc), searcher=email)
        return 0
