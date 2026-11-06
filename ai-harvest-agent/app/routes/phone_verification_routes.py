"""
Phone call verification routes ("Verify mobile number" on the Harvested Jobs table).

Endpoints
─────────
POST   /jobs/{id}/phone-verification          — log a call (outcome, details, proof, reminder)
DELETE /jobs/{id}/phone-verification?phone=   — "Clear status" for that number
GET    /jobs/{id}/phone-verification/history  — every non-cleared call, newest first
GET    /phone-verifications/latest?job_ids=   — newest call per visible job (badges)
                                               + flags of those numbers
GET    /phone-verifications/flags?phones=     — invalid / do-not-call / warm per number
GET    /phone-verifications/follow-ups        — the caller's due follow-up reminders
POST   /phone-verifications/follow-ups/{id}/done — dismiss one reminder
POST   /phone-verifications/follow-ups/done-all  — dismiss all due reminders

The per-job endpoints honour the self-assignment lock (ensure_job_access → 403
when the job is assigned to someone else); /latest omits such jobs. Call views
are documented on phone_verification_service.call_view, number flags on
phone_verification_service.flag_view (keyed by the number as given).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user, get_db_session
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


def _split_phones(raw: str) -> list[str]:
    if not isinstance(raw, str):  # a direct (non-HTTP) call gets the Query() default
        return []
    phones = [p.strip() for p in raw.split(",") if p.strip()]
    return list(dict.fromkeys(phones))[:pvs.FLAGS_MAX_PHONES]


async def _load_job_guarded(
    db: AsyncSession, job_id: str, user: AuthenticatedUser
) -> ScrapedJobORM:
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
               422: {"description": "Invalid status / outcome, missing outcome "
                                     "fields or proof of call"}},
)
async def log_phone_call(
    job_id: str,
    body: LogCallRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
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
    ids = [j.strip() for j in job_ids.split(",") if j.strip()]
    ids = list(dict.fromkeys(ids))[:LATEST_MAX_IDS]
    extra_phones = _split_phones(phones)
    if not ids:
        return {"items": {}, "flags": await pvs.flags_for_phones(db, extra_phones)}

    # Drop jobs locked to someone else (same rule as ensure_job_access).
    me = (current_user.email or "").lower()
    locks = (await db.execute(apply_tenant(
        select(ScrapedJobORM.id, ScrapedJobORM.assigned_to_email)
        .where(ScrapedJobORM.id.in_(ids)),
        ScrapedJobORM.tenant_id,
    ))).all()
    locked = {jid for jid, owner in locks if owner and owner.lower() != me}
    visible = [j for j in ids if j not in locked]
    items = await pvs.latest_for_jobs(db, visible)
    flags = await pvs.flags_for_phones(db, [v["phone"] for v in items.values()] + extra_phones)
    return {"items": items, "flags": flags}


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
    row = await pvs.dismiss_followup(db, log_id, current_user)
    return {"id": row.id, "done": True}
