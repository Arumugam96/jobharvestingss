"""
Phone call verification routes ("Verify mobile number" on the Harvested Jobs table).

Endpoints
─────────
POST   /jobs/{id}/phone-verification          — log a call (outcome, details, proof, reminder)
DELETE /jobs/{id}/phone-verification?phone=   — "Clear status" for that number
GET    /jobs/{id}/phone-verification/history  — every non-cleared call, newest first
GET    /phone-verifications/latest?job_ids=   — newest call per visible job (badges)
                                               + flags and per-number call summaries
                                               (`contacts`, across all jobs) of those numbers
GET    /phone-verifications/contact-history?phone= — every call on a number, across jobs
GET    /phone-verifications/logs              — every call in the tenant (admins; filter by
                                               assignee) or a business lead's own calls
PATCH  /phone-verifications/logs/{id}         — edit one logged call in place (admins: any;
                                               business leads: their own)
GET    /phone-verifications/assignees         — business leads for the assignee filter (admins)
GET    /phone-verifications/logs/contacts     — "By contact": one row per number (admins:
                                               all; business leads: numbers they called)
GET    /phone-verifications/logs/phones       — every phone number held (admins only)
GET    /phone-verifications/logs/assigned-jobs — every self-assigned job (admins only)
POST   /phone-verifications/logs/xlsx         — the page's export rows as .xlsx (admins + leads)
GET    /phone-verifications/flags?phones=     — invalid / do-not-call / warm per number
GET    /phone-verifications/follow-ups        — the caller's due follow-up reminders
POST   /phone-verifications/follow-ups/{id}/done — dismiss one reminder
POST   /phone-verifications/follow-ups/done-all  — dismiss all due reminders

The per-job endpoints honour the self-assignment lock (ensure_job_access → 403
when the job is assigned to someone else); /latest omits such jobs except for
admins, who get read-only visibility of them. Call views
are documented on phone_verification_service.call_view, number flags on
phone_verification_service.flag_view (keyed by the number as given).

Ownership: logging a call stamps the row's owner (assigned_to_user_id) with the
authenticated caller and self-assigns a still-unassigned job to them — the client
never supplies an assignee. Business leads read only the logs they own; the scope
is applied in SQL here (logs_owner_scope), not in the UI.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import (
    get_current_user,
    get_db_session,
    require_admin,
    require_phone_logs_access,
)
from app.core.tenant_context import apply_tenant
from app.models.auth import AuthenticatedUser
from app.models.harvest_run import ScrapedJobORM
from app.services import phone_verification_service as pvs
from app.services.harvest_run_service import HarvestRunService
from app.services.job_assignment_service import ensure_job_access

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Jobs & Leads"])

LATEST_MAX_IDS = 200
_GUARDED_RESPONSES: dict = {
    403: {"description": "Assigned to someone else"},
    404: {"description": "Job not found"},
}


class ProofFile(BaseModel):
    name: str = Field(default="", max_length=255)
    size: str = Field(default="", max_length=20)
    type: str = Field(default="", max_length=40, description="e.g. Call log screenshot")
    src: str = Field(default="", description="Small image as a data: URL, or blank.")


class LogCallRequest(BaseModel):
    phone: str = Field(..., min_length=1, max_length=pvs.PHONE_MAX_LEN,
                       description="Number as dialed.")
    phone_source: str = Field(default="", max_length=pvs.PHONE_SOURCE_MAX_LEN,
                              description="job | recruiter | contact | company")
    contact_name: str = Field(default="", max_length=pvs.CONTACT_NAME_MAX_LEN)
    status: str = Field(..., description="answered | unanswered")
    outcome: str = Field(default="", max_length=40,
                         description="Outcome under the status, e.g. connect_back, switched_off.")
    details: dict[str, Any] = Field(default_factory=dict,
                                    description="Outcome-specific fields (next steps, reason…).")
    proof: list[ProofFile] = Field(default_factory=list, max_length=pvs.PROOF_MAX_FILES)
    remarks: str = Field(default="", description="Required for an answered call without an outcome.")
    reminder_at: datetime | None = Field(default=None,
                                         description="Remind me at this time (any status).")
    follow_up: bool = Field(default=False,
                            description="Older clients: remind me in 24h (unanswered calls only).")


class UpdateCallRequest(BaseModel):
    """PATCH body: LogCallRequest's editable fields (phone / phone_source / contact_name
    are fixed once logged)."""
    status: str = Field(..., description="answered | unanswered")
    outcome: str = Field(default="", max_length=40)
    details: dict[str, Any] = Field(default_factory=dict)
    proof: list[ProofFile] = Field(default_factory=list, max_length=pvs.PROOF_MAX_FILES)
    remarks: str = Field(default="")
    reminder_at: datetime | None = Field(default=None)
    follow_up: bool = Field(default=False)


def _split_phones(raw: str) -> list[str]:
    """Split a comma-separated phone list, trimming blanks and duplicates and capping the count.

    Returns [] for a non-string (a direct, non-HTTP call receives the Query() default object).
    """
    if not isinstance(raw, str):  # a direct (non-HTTP) call gets the Query() default
        return []
    phones = [p.strip() for p in raw.split(",") if p.strip()]
    return list(dict.fromkeys(phones))[:pvs.FLAGS_MAX_PHONES]


async def _load_job_guarded(
    db: AsyncSession, job_id: str, user: AuthenticatedUser
) -> ScrapedJobORM:
    """Load the job for a per-job endpoint: 404 if missing, 403 if assigned to someone else."""
    job = await HarvestRunService(db).get_scraped_job_by_id(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found"
        )
    ensure_job_access(job.assigned_to_email, user)
    return job


@router.post(
    "/jobs/{job_id}/phone-verification",
    status_code=status.HTTP_200_OK,
    summary="Log a phone call on a job",
    responses={403: {"description": "Assigned to someone else"},
               404: {"description": "Job not found"},
               422: {"description": "Invalid status / outcome or missing required call fields"}},
)
async def log_phone_call(
    job_id: str,
    body: LogCallRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Log one call on the job (status, outcome, details, proof, optional reminder).

    Closes any open follow-up on the same job + number before inserting the new row. Returns the
    call view plus `attempts` (non-cleared calls on the job) and the number's `flags`. 403/404 per
    the assignment lock; 422 for an invalid status/outcome, missing required call fields, or oversized details.
    Proof attachments are optional; an empty proof list is accepted.
    """
    job = await _load_job_guarded(db, job_id, current_user)
    row = await pvs.log_call(
        db, job, current_user,
        phone=body.phone, phone_source=body.phone_source, contact_name=body.contact_name,
        status=body.status, remarks=body.remarks, follow_up=body.follow_up,
        outcome=body.outcome, details=body.details,
        proof=[p.model_dump() for p in body.proof], reminder_at=body.reminder_at,
    )
    logger.info("phone_call_logged", job_id=job_id, user=current_user.email,
                status=row.call_status, outcome=row.call_outcome,
                follow_up_due=bool(row.follow_up_due_at))
    counts = await pvs.count_calls(db, [job_id])
    flags = await pvs.flags_for_phones(db, [row.phone])
    return {**pvs.call_view(row), "attempts": counts.get(job_id, 1),
            "flags": flags.get(row.phone, pvs.flag_view(None))}


@router.delete(
    "/jobs/{job_id}/phone-verification",
    status_code=status.HTTP_200_OK,
    summary="Clear a job's call status for a number",
    responses=_GUARDED_RESPONSES,
)
async def clear_phone_call(
    job_id: str,
    phone: str = Query(default="",
                       description="Number to clear; blank clears every number on the job."),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """"Clear status": soft-delete the job's active calls for `phone` (every number when blank).

    Also closes their open follow-ups. Returns `{"job_id", "cleared": <rows cleared>}`; 403/404
    per the assignment lock.
    """
    await _load_job_guarded(db, job_id, current_user)
    cleared = await pvs.clear_calls(db, job_id, phone)
    logger.info("phone_call_cleared", job_id=job_id, user=current_user.email, cleared=cleared)
    return {"job_id": job_id, "cleared": cleared}


@router.get(
    "/jobs/{job_id}/phone-verification/history",
    summary="Call history for a job",
    responses=_GUARDED_RESPONSES,
)
async def phone_call_history(
    job_id: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Every non-cleared call on the job, newest first; 403/404 per the assignment lock."""
    await _load_job_guarded(db, job_id, current_user)
    return {"job_id": job_id, "items": await pvs.history(db, job_id)}


@router.get("/phone-verifications/latest", summary="Latest call per job (batch, for table badges)")
async def latest_phone_verifications(
    job_ids: str = Query(default="", description="Comma-separated scraped job ids (max 200)."),
    phones: str = Query(default="",
                        description="Comma-separated numbers to return flags for too (max 200)."),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Newest non-cleared call per job, for the table's status badges, plus number flags.

    `job_ids` and `phones` are comma-separated and capped at 200. For non-admins, jobs assigned to
    someone else are silently omitted; admins see them read-only. `flags` and `contacts` cover the
    extra `phones` plus the numbers of the returned calls. Returns
    `{"items": {job_id: call}, "flags": {phone: flag_view}, "contacts": {phone: summary}}` where a
    summary is `{calls, jobs, latest: {id, status, outcome, reason, revisit, called_by, called_at,
    job_id, job_title, company, contact_name, phone}}` over every caller's calls to that number on
    any job (pvs.contact_summaries) — the "Contacted" badge. Numbers never called are absent.
    """
    ids = [j.strip() for j in job_ids.split(",") if j.strip()]
    ids = list(dict.fromkeys(ids))[:LATEST_MAX_IDS]
    extra_phones = _split_phones(phones)
    if not ids:
        return {"items": {}, "flags": await pvs.flags_for_phones(db, extra_phones),
                "contacts": await pvs.contact_summaries(db, extra_phones)}

    # Drop jobs locked to someone else (same rule as ensure_job_access). Admins
    # keep them: they read the assignee's call results (read-only — every write
    # endpoint above still goes through ensure_job_access).
    visible = ids
    if not current_user.is_admin:
        me = (current_user.email or "").lower()
        locks = (await db.execute(apply_tenant(
            select(ScrapedJobORM.id, ScrapedJobORM.assigned_to_email)
            .where(ScrapedJobORM.id.in_(ids)),
            ScrapedJobORM.tenant_id,
        ))).all()
        locked = {jid for jid, owner in locks if owner and owner.lower() != me}
        visible = [j for j in ids if j not in locked]
    items = await pvs.latest_for_jobs(db, visible)
    numbers = list(dict.fromkeys(extra_phones + [v["phone"] for v in items.values()]))
    flags = await pvs.flags_for_phones(db, numbers)
    contacts = await pvs.contact_summaries(db, numbers)
    return {"items": items, "flags": flags, "contacts": contacts}


@router.get("/phone-verifications/contact-history",
            summary="Every call to one number, across jobs (team-wide)")
async def contact_call_history(
    phone: str = Query(..., min_length=1, max_length=pvs.PHONE_MAX_LEN),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Every non-cleared call on this number (matched on its trailing 10 digits) on any job
    in the tenant, newest first, by any caller — so a business lead sees a teammate's
    outcome before calling the same recruiter again. Informational only; nothing blocks.

    Returns `{"phone", "key", "calls", "jobs", "items": [...]}`; each item is
    `{id, status, outcome, reason, revisit, called_by, called_at, job_id, job_title, company,
    contact_name, phone, details, remarks, proof, can_edit}`. Proof images (`src`) are blank
    unless the viewer owns that log or is an admin (pvs.can_edit_log)."""
    return await pvs.contact_history(db, phone, current_user)


def logs_owner_scope(user: AuthenticatedUser, assignee: str) -> tuple[str | None, str | None]:
    """(owner_id, own_email) for list_logs from the caller's role.

    Admins see everything, narrowed to one owner when `assignee` is given. Business
    leads are always pinned to their own rows; asking for anyone else's is a 403, so a
    tampered URL can't widen the scope.
    """
    # A direct (non-HTTP) call gets the Query() default object, not a str.
    assignee = assignee.strip() if isinstance(assignee, str) else ""
    if user.is_admin:
        return (assignee or None), None
    if assignee and assignee != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="You can only view your own phone logs.")
    return user.id, (user.email or "")


@router.get("/phone-verifications/logs",
            summary="Logged calls: all (admins) or your own (business leads)")
async def phone_call_logs(
    status_filter: str = Query(default="", alias="status", pattern="^(|answered|unanswered)$"),
    assignment: str = Query(default="", pattern="^(|assigned|unassigned)$"),
    reminder: str = Query(
        default="", pattern="^(|none|scheduled|delivered|dismissed|called_again|cancelled)$"),
    days: int = Query(default=0, ge=0, le=3650, description="Only calls from the last N days; 0 = all."),
    q: str = Query(default="", max_length=200),
    assignee: str = Query(default="", max_length=36,
                          description="Owner user id (admins only; a business lead is "
                                      "always scoped to their own calls)."),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    current_user: AuthenticatedUser = Depends(require_phone_logs_access),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Phone logs page: non-cleared calls, newest first, paginated.

    Admins get every call in the tenant (optionally only those owned by `assignee`);
    business leads get only the calls they own (`scope: "own"`) — 403 if they pass another
    user's `assignee`. Filters: `status` (answered/unanswered), `assignment`
    (assigned/unassigned), `reminder` (a REMINDER_STATES value), `days` (0 = all time) and
    free-text `q`. Other roles get 403.
    """
    owner_id, own_email = logs_owner_scope(current_user, assignee)
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    return await pvs.list_logs(db, status=status_filter, assignment=assignment,
                               reminder=reminder, q=q, since=since, page=page,
                               page_size=page_size, owner_id=owner_id, own_email=own_email,
                               viewer=current_user)


@router.get("/phone-verifications/logs/contacts",
            summary="Phone logs by contact: one row per number (admins: all; leads: their numbers)")
async def phone_log_contacts(
    status_filter: str = Query(default="", alias="status", pattern="^(|answered|unanswered)$"),
    assignment: str = Query(default="", pattern="^(|assigned|unassigned)$"),
    reminder: str = Query(
        default="", pattern="^(|none|scheduled|delivered|dismissed|called_again|cancelled)$"),
    days: int = Query(default=0, ge=0, le=3650),
    q: str = Query(default="", max_length=200),
    assignee: str = Query(default="", max_length=36),
    outcome: str = Query(default="", max_length=40,
                         description="Only numbers whose LATEST call has this outcome."),
    revisit_due: bool = Query(default=False,
                              description="Only numbers whose latest call is not_interested "
                                          "and its revisit window has passed."),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    current_user: AuthenticatedUser = Depends(require_phone_logs_access),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """The Phone logs "By contact" view, grouped and paginated server-side.

    The Calls view's filters pick the numbers (any matching call); a business lead only
    gets numbers they called (403 for another `assignee`), but each row's figures include
    teammates' calls on that number. Returns `{"items", "total", "page", "page_size",
    "scope"}`; each item is `{key, phone, contact_name, company, calls, jobs,
    first_called_at, latest: {id, status, outcome, reason, revisit, called_by, called_at,
    job_id, job_title, company, contact_name, phone}}`. Expand a row with
    GET /phone-verifications/contact-history."""
    owner_id, own_email = logs_owner_scope(current_user, assignee)
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    return await pvs.list_contacts(
        db, status=status_filter, assignment=assignment, reminder=reminder, q=q, since=since,
        outcome=(outcome or "").strip().lower(), revisit_due=revisit_due, page=page,
        page_size=page_size, owner_id=owner_id, own_email=own_email,
    )


@router.patch(
    "/phone-verifications/logs/{log_id}",
    status_code=status.HTTP_200_OK,
    summary="Edit a logged call in place (admins: any; business leads: their own)",
    responses={403: {"description": "Not your call log"},
               404: {"description": "Call log not found (or cleared)"},
               422: {"description": "Invalid status / outcome or missing required call fields"}},
)
async def update_phone_call_log(
    log_id: int,
    body: UpdateCallRequest,
    current_user: AuthenticatedUser = Depends(require_phone_logs_access),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Update one call log row (status, outcome, details, remarks, proof, reminder) —
    never inserts a new one. Same call-field validation as logging a call. Proof attachments are optional. Returns the row shaped
    exactly like one GET /phone-verifications/logs item."""
    item = await pvs.update_call(
        db, log_id, current_user,
        status=body.status, remarks=body.remarks, follow_up=body.follow_up,
        outcome=body.outcome, details=body.details,
        proof=[p.model_dump() for p in body.proof], reminder_at=body.reminder_at,
    )
    logger.info("phone_call_log_updated", log_id=log_id, user=current_user.email,
                status=item["status"], outcome=item["outcome"])
    return item


@router.get("/phone-verifications/assignees",
            summary="Business leads for the Phone logs assignee filter (admins only)")
async def phone_log_assignees(
    q: str = Query(default="", max_length=200, description="Search by name or email."),
    current_user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """`{"items": [{id, name, email}]}` — active business-lead users in the caller's tenant
    scope, sorted by name. Admins only."""
    return {"items": await pvs.list_assignees(db, q)}


@router.get("/phone-verifications/logs/phones",
            summary="Every phone number in the tenant (admins only)")
async def phone_inventory_list(
    source: str = Query(default="", pattern="^(|job|company|recruiter)$"),
    q: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    current_user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """The Phone logs "Total phones" drill-down: job poster, company HR and recruiter
    numbers (see phone_verification_service.phone_inventory), paginated. Admins only."""
    return await pvs.list_phones(db, source=source, q=q, page=page, page_size=page_size)


@router.get("/phone-verifications/logs/assigned-jobs",
            summary="Every self-assigned job in the tenant (admins only)")
async def assigned_jobs_list(
    q: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    current_user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """The Phone logs "Self-assigned jobs" drill-down: every currently assigned job,
    called or not, with its call count and latest call. Admins only."""
    return await pvs.list_assigned_jobs(db, q=q, page=page, page_size=page_size)


class XlsxExportRequest(BaseModel):
    sheet: str = Field(default="Phone logs", max_length=60)
    filename: str = Field(default="phone-logs.xlsx", max_length=120)
    head: list[str] = Field(..., min_length=1, max_length=pvs.EXPORT_MAX_COLS)
    rows: list[list[Any]] = Field(default_factory=list, max_length=pvs.EXPORT_MAX_ROWS)


@router.post("/phone-verifications/logs/xlsx",
             summary="Phone logs list as an Excel file (admins and business leads)",
             response_class=Response,
             responses={200: {"description": "Excel file download"}})
async def phone_logs_xlsx(
    body: XlsxExportRequest,
    current_user: AuthenticatedUser = Depends(require_phone_logs_access),
) -> Response:
    """The Phone logs page's Excel export: the page sends the rows it would put in its
    CSV (the on-screen list, every page, with its filters) and gets them back as .xlsx.
    It only re-formats rows the caller sent, so a business lead can export their own."""
    data = pvs.rows_to_xlsx(body.sheet, body.head, body.rows)
    name = re.sub(r"[^A-Za-z0-9._-]", "-", body.filename) or "phone-logs.xlsx"
    if not name.lower().endswith(".xlsx"):
        name += ".xlsx"
    logger.info("phone_logs_xlsx_export", user=current_user.email, rows=len(body.rows))
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/phone-verifications/flags", summary="Call-learned flags per number")
async def phone_flags(
    phones: str = Query(default="", description="Comma-separated numbers (max 200)."),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """{phone: {invalid, dnc, warm, reenrich_requested, correct_number}} — only
    numbers carrying a flag are listed."""
    return {"flags": await pvs.flags_for_phones(db, _split_phones(phones))}


@router.get("/phone-verifications/follow-ups", summary="My due follow-up call reminders")
async def due_call_followups(
    limit: int = Query(default=20, ge=1, le=100),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """The caller's follow-up reminders that are now due, newest due first.

    Returns `{"due": <total count>, "items": [...]}` with at most `limit` items.
    """
    due, items = await pvs.due_followups(db, current_user, limit)
    return {"due": due, "items": items}


@router.post(
    "/phone-verifications/follow-ups/done-all",
    status_code=status.HTTP_200_OK,
    summary="Dismiss all my due follow-up reminders",
)
async def dismiss_all_call_followups(
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Mark every currently due follow-up of the caller done; returns `{"updated": <rows>}`."""
    updated = await pvs.dismiss_all(db, current_user)
    return {"updated": updated}


@router.post(
    "/phone-verifications/follow-ups/{log_id}/done",
    status_code=status.HTTP_200_OK,
    summary="Dismiss one follow-up reminder",
    responses={403: {"description": "Not your call"}, 404: {"description": "Call log not found"}},
)
async def dismiss_call_followup(
    log_id: int,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Mark one follow-up reminder done; 404 if the call log is missing, 403 if it isn't yours."""
    row = await pvs.dismiss_followup(db, log_id, current_user)
    return {"id": row.id, "done": True}
