"""Recruiter outreach API — LLM-generated email + LinkedIn messages composed
from the Harvested Jobs table, sent over the existing SMTP transport.

Endpoints (Swagger-visible, session-authenticated like the other public routers):
  POST /outreach/generate-email      draft a recruiter email (tone + audience aware)
  POST /outreach/generate-followup   draft a follow-up email from the prior outreach
  POST /outreach/generate-linkedin   draft a LinkedIn outreach message
  POST /outreach/send-email          send a (possibly edited) email; dedup-guarded, logged
  POST /outreach/log-linkedin        record a manually-sent LinkedIn message
  GET  /outreach/status              latest sent state per job (row icons)
  GET  /outreach/history             outreach thread for a job/recruiter, or recent list

Generation is audited in the shared `llm_calls` table (call_type email_generation /
email_followup / linkedin_generation, run_id NULL). Sends (email + linkedin) are logged
in `email_outreach`, which is the source of truth for the "already contacted" state.
"""
from __future__ import annotations

import re
from uuid import uuid4

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.dependencies import get_current_user, get_db_session, get_email_sender, get_llm_service
from app.models.auth import AuthenticatedUser
from app.models.harvest_run import LlmCallType
from app.models.outreach import EmailOutreachORM
from app.services.active_clients import classify_client
from app.services.email_service import EmailSender
from app.services.harvest_run_service import HarvestRunService, insert_llm_call, scraped_job_view
from app.services.llm_service import LLMService
from app.services.outreach_service import OutreachService
from app.services.outreach_log_service import (
    contact_names_for_rows,
    get_by_id,
    initial_email_sent,
    latest_sent_email,
    outreach_to_dict,
    recent_outreach,
    record_delivery_events,
    sent_status_for_jobs,
    thread_messages,
)
from app.prompts.outreach_prompts import TONES

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/outreach", tags=["Outreach"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ── Request models ───────────────────────────────────────────────────────────

class GenerateEmailRequest(BaseModel):
    job_id: str = Field(..., description="Scraped job id (the row's id).")
    mode: str = Field(default="Formal", description="Tone: Formal | Friendly | Direct.")
    regenerate: bool = Field(default=False, description="Ask for a distinctly different variation.")


class GenerateLinkedinRequest(BaseModel):
    job_id: str = Field(..., description="Scraped job id (the row's id).")
    regenerate: bool = Field(default=False, description="Ask for a distinctly different variation.")


class SendEmailRequest(BaseModel):
    to_email: str = Field(..., description="Recipient address (editable in the UI).")
    from_email: str = Field(..., description="Visible sender address (editable in the UI).")
    subject: str = Field(..., description="Email subject.")
    body: str = Field(..., description="Plain-text email body.")
    job_id: str | None = Field(default=None, description="Source job id, for logging/classification.")
    tone: str = Field(default="", description="Tone the body was generated with.")
    client_type: str = Field(default="", description="active | new | unknown (recomputed if job_id given).")
    fallback_used: bool = Field(default=False, description="True if the body came from the static template.")
    parent_outreach_id: str | None = Field(
        default=None, description="Set when this send is a follow-up to an earlier outreach row."
    )
    force: bool = Field(
        default=False, description="Bypass the duplicate-initial-send guard (user confirmed a re-send)."
    )


class GenerateFollowupRequest(BaseModel):
    job_id: str = Field(..., description="Scraped job id (the row's id).")
    parent_outreach_id: str | None = Field(
        default=None, description="The outreach being followed up; defaults to the latest sent email for the job."
    )
    mode: str = Field(default="Formal", description="Tone: Formal | Friendly | Direct.")
    regenerate: bool = Field(default=False, description="Ask for a distinctly different variation.")


class LogLinkedinRequest(BaseModel):
    job_id: str = Field(..., description="Scraped job id (the row's id).")
    message: str = Field(default="", description="The LinkedIn message text the user copied and sent manually.")


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _load_job_view(db: AsyncSession, job_id: str) -> dict:
    job = await HarvestRunService(db).get_scraped_job_by_id(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    view = scraped_job_view(job)
    view["_recruiter_id"] = job.recruiter_id  # ORM-only field, not in the public view
    return view


# ═══════════════════════════════════════════════════════════════════════════════
# POST /outreach/generate-email
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/generate-email", status_code=status.HTTP_200_OK)
async def generate_email(
    body: GenerateEmailRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    llm_service: LLMService = Depends(get_llm_service),
) -> dict:
    """Draft a recruiter outreach email for a job. Audience (active/new/unknown)
    is derived from the company; tone is the requested mode. Records the LLM call
    in `llm_calls` (call_type=email_generation)."""
    view = await _load_job_view(db, body.job_id)
    tone = body.mode if body.mode in TONES else "Formal"
    client_type = classify_client(view.get("company") or "")
    deck_url = get_settings().outreach_deck_url

    draft = await OutreachService(llm_service).generate_email(
        view, client_type, tone, regenerate=body.regenerate,
        sender_email=current_user.email, deck_url=deck_url,
    )

    await insert_llm_call(
        db,
        call_type=LlmCallType.EMAIL_GENERATION,
        provider=draft.meta.provider,
        model=draft.meta.model,
        prompt=draft.meta.prompt,
        response=draft.meta.response,
        input_tokens=draft.meta.input_tokens,
        output_tokens=draft.meta.output_tokens,
        latency_ms=draft.meta.latency_ms,
        success=draft.meta.success,
        error_message=draft.meta.error_message,
        job_url=view.get("job_url"),
    )

    return {
        "subject": draft.subject,
        "body": draft.body,
        "from_email": current_user.email,
        "to_email": view.get("email_id") or "",
        "client_type": client_type,
        "tone": tone,
        "fallback_used": draft.fallback_used,
        "deck_url": deck_url,
        # Sent to the composer so its Preview can render the job title as the same
        # bold blue new-tab link the delivered email gets (see _outreach_body_to_html).
        "job_title": view.get("job_title") or "",
        "job_url": view.get("job_url") or "",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# POST /outreach/generate-linkedin
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/generate-linkedin", status_code=status.HTTP_200_OK)
async def generate_linkedin(
    body: GenerateLinkedinRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    llm_service: LLMService = Depends(get_llm_service),
) -> dict:
    """Draft a single generic LinkedIn outreach message for a job. Records the
    LLM call in `llm_calls` (call_type=linkedin_generation)."""
    view = await _load_job_view(db, body.job_id)

    draft = await OutreachService(llm_service).generate_linkedin(view, regenerate=body.regenerate)

    await insert_llm_call(
        db,
        call_type=LlmCallType.LINKEDIN_GENERATION,
        provider=draft.meta.provider,
        model=draft.meta.model,
        prompt=draft.meta.prompt,
        response=draft.meta.response,
        input_tokens=draft.meta.input_tokens,
        output_tokens=draft.meta.output_tokens,
        latency_ms=draft.meta.latency_ms,
        success=draft.meta.success,
        error_message=draft.meta.error_message,
        job_url=view.get("job_url"),
    )

    return {"message": draft.message, "fallback_used": draft.fallback_used}


# ═══════════════════════════════════════════════════════════════════════════════
# POST /outreach/send-email
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/send-email", status_code=status.HTTP_200_OK)
async def send_email(
    body: SendEmailRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    email_sender: EmailSender = Depends(get_email_sender),
) -> dict:
    """Send a (possibly edited) outreach email with the corporate-overview pptx
    attached. The visible From is the shared "harvest agent" identity (same
    sender as the OTP email); the composer's address (`from_email`) becomes the
    Reply-To so the recruiter's reply reaches the salesperson. Always returns 200
    with a `status` so the failed row is persisted (raising would roll back the
    session)."""
    to_email = (body.to_email or "").strip()
    from_email = (body.from_email or "").strip()
    logger.info(f"the Email from {from_email} is sent to {to_email}")
    if not _EMAIL_RE.match(to_email):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid recipient email")
    if from_email and not _EMAIL_RE.match(from_email):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid sender email")
    if not (body.subject or "").strip() or not (body.body or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Subject and body are required")

    # Recompute company / recruiter link / audience from the job when available.
    company, recruiter_id, client_type = "", None, body.client_type or "unknown"
    job_title, job_url = "", ""
    if body.job_id:
        job = await HarvestRunService(db).get_scraped_job_by_id(body.job_id)
        if job is not None:
            company = job.company or ""
            recruiter_id = job.recruiter_id
            client_type = classify_client(company)
            job_title = job.job_title or ""
            job_url = job.job_url or ""

    # A send is a follow-up when it's linked to a prior outreach; otherwise it's an
    # initial contact. Generate ≠ Sent — this row is only written after the SMTP
    # attempt below, and status="sent" only on a real success.
    outreach_kind = "followup" if body.parent_outreach_id else "initial"

    # Duplicate-send guard (initial only): if this job/recruiter was already
    # emailed once and the user hasn't explicitly confirmed a re-send, don't send
    # again — report the existing send so the UI can ask for confirmation.
    if outreach_kind == "initial" and not body.force:
        existing = await initial_email_sent(db, job_id=body.job_id, recruiter_id=recruiter_id)
        if existing is not None:
            return {"status": "duplicate", "existing": outreach_to_dict(existing)}

    # No file attachment: the corporate-overview deck is delivered as a link in the
    # body (OUTREACH_DECK_URL) rather than a ~7 MB attachment, keeping the send fast.
    # Pre-generate the row id so it can ride along as the Mailjet CustomID: delivery
    # event webhooks echo it back, letting us map events to this exact send.
    outreach_id = str(uuid4())
    send_status, error_message, message_id = "sent", None, None
    try:
        message_id = await email_sender.send_email_with_attachments(
            recipients=[to_email],
            subject=body.subject,
            body=body.body,
            from_email=from_email or None,
            reply_to=from_email or None,
            as_html=True,
            job_title=job_title,
            job_url=job_url,
            custom_id=outreach_id,
        )
    except Exception as exc:  # Mailjet/config failure — record and report, don't 500
        send_status, error_message = "failed", str(exc)
        logger.warning("outreach_send_failed", to=to_email, error=str(exc))

    row = EmailOutreachORM(
        id=outreach_id,
        job_id=body.job_id,
        recruiter_id=recruiter_id,
        channel="email",
        outreach_kind=outreach_kind,
        parent_outreach_id=body.parent_outreach_id,
        provider_message_id=message_id,
        company=company,
        client_type=client_type,
        tone=body.tone or "",
        to_email=to_email,
        from_email=from_email,
        subject=body.subject,
        body=body.body,
        attachment_name="",
        llm_generated=not body.fallback_used,
        fallback_used=body.fallback_used,
        status=send_status,
        error_message=error_message,
        # Seed the delivery lifecycle: a clean hand-off to Mailjet starts at "sent";
        # the event webhook advances it to delivered/opened/bounced. A failed send
        # (never reached Mailjet) leaves it NULL.
        delivery_status="sent" if send_status == "sent" else None,
        sent_by=current_user.email,
    )
    db.add(row)
    await db.flush()

    return {"status": send_status, "error": error_message, "outreach_id": row.id}


# ═══════════════════════════════════════════════════════════════════════════════
# POST /outreach/generate-followup
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/generate-followup", status_code=status.HTTP_200_OK)
async def generate_followup(
    body: GenerateFollowupRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
    llm_service: LLMService = Depends(get_llm_service),
) -> dict:
    """Draft a follow-up outreach email for a job, using the prior outreach as
    context so it reads as a second-touch nudge rather than a fresh introduction.
    The prior message is the explicit `parent_outreach_id` if given, else the
    latest successfully-sent email for the job/recruiter. Records the LLM call in
    `llm_calls` (call_type=email_followup)."""
    view = await _load_job_view(db, body.job_id)
    tone = body.mode if body.mode in TONES else "Formal"
    client_type = classify_client(view.get("company") or "")
    deck_url = get_settings().outreach_deck_url

    prior = None
    if body.parent_outreach_id:
        prior = await get_by_id(db, body.parent_outreach_id)
    if prior is None:
        prior = await latest_sent_email(db, job_id=body.job_id, recruiter_id=view.get("_recruiter_id"))

    prior_subject = prior.subject if prior else ""
    prior_body = prior.body if prior else ""
    prior_sent_at = prior.created_at.isoformat() if (prior and prior.created_at) else ""

    draft = await OutreachService(llm_service).generate_followup_email(
        view, client_type, tone,
        prior_subject=prior_subject, prior_body=prior_body, prior_sent_at=prior_sent_at,
        regenerate=body.regenerate, sender_email=current_user.email, deck_url=deck_url,
    )

    await insert_llm_call(
        db,
        call_type=LlmCallType.EMAIL_FOLLOWUP,
        provider=draft.meta.provider,
        model=draft.meta.model,
        prompt=draft.meta.prompt,
        response=draft.meta.response,
        input_tokens=draft.meta.input_tokens,
        output_tokens=draft.meta.output_tokens,
        latency_ms=draft.meta.latency_ms,
        success=draft.meta.success,
        error_message=draft.meta.error_message,
        job_url=view.get("job_url"),
    )

    return {
        "subject": draft.subject,
        "body": draft.body,
        "from_email": current_user.email,
        # Default the recipient to whoever received the prior email, else the job's contact.
        "to_email": (prior.to_email if prior else "") or view.get("email_id") or "",
        "client_type": client_type,
        "tone": tone,
        "fallback_used": draft.fallback_used,
        "deck_url": deck_url,
        "job_title": view.get("job_title") or "",
        "job_url": view.get("job_url") or "",
        # Echoed so the composer links the follow-up send back to its parent row.
        "parent_outreach_id": (prior.id if prior else None),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# POST /outreach/log-linkedin  — record a manually-sent LinkedIn message
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/log-linkedin", status_code=status.HTTP_200_OK)
async def log_linkedin_sent(
    body: LogLinkedinRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Record that a LinkedIn outreach message was sent to a recruiter. There is
    no programmatic LinkedIn transport — the user copies the generated message,
    sends it in LinkedIn, then marks it sent here, which writes a channel="linkedin"
    send-log row so the UI icon reflects a real, DB-backed sent state."""
    job = await HarvestRunService(db).get_scraped_job_by_id(body.job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Job {body.job_id} not found")

    company = job.company or ""
    row = EmailOutreachORM(
        job_id=body.job_id,
        recruiter_id=job.recruiter_id,
        channel="linkedin",
        outreach_kind="initial",
        company=company,
        client_type=classify_client(company),
        tone="",
        to_email="",
        from_email="",
        subject="",
        body=body.message or "",
        attachment_name="",
        llm_generated=True,
        fallback_used=False,
        status="sent",
        error_message=None,
        sent_by=current_user.email,
    )
    db.add(row)
    await db.flush()
    return {"status": "sent", "outreach_id": row.id}


# ═══════════════════════════════════════════════════════════════════════════════
# GET /outreach/status  — latest sent state per job (powers the row icons)
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/status", status_code=status.HTTP_200_OK)
async def outreach_status(
    job_ids: str = Query(..., description="Comma-separated scraped job ids."),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Batch lookup of the latest successful outreach per job, split by channel —
    the source of truth for the sent/not-sent icons. Returns
    { job_id: { "email": {...}, "linkedin": {...} } } for jobs that have any sent row."""
    ids = [x.strip() for x in (job_ids or "").split(",") if x.strip()]
    return await sent_status_for_jobs(db, ids)


# ═══════════════════════════════════════════════════════════════════════════════
# GET /outreach/history  — outreach thread for a job/recruiter, or recent list
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/history", status_code=status.HTTP_200_OK)
async def outreach_history(
    job_id: str | None = Query(default=None, description="Filter to one job's outreach thread."),
    recruiter_id: str | None = Query(default=None, description="Filter to one recruiter's outreach thread."),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """With a job_id/recruiter_id: the ordered outreach thread (initial + follow-ups
    + linkedin) for that contact. With neither: the recent-outreach list for the
    Mail logs page. Each item carries the resolved contact_name (Contact column)
    plus the Mailjet delivery-engagement fields."""
    if job_id or recruiter_id:
        rows = await thread_messages(db, job_id=job_id, recruiter_id=recruiter_id, limit=limit)
    else:
        rows = await recent_outreach(db, limit=limit)
    names = await contact_names_for_rows(db, rows)
    return {"items": [outreach_to_dict(r, contact_name=names.get(r.job_id)) for r in rows]}


# ═══════════════════════════════════════════════════════════════════════════════
# POST /outreach/mailjet-events — Mailjet delivery-event webhook (unauthenticated)
# ═══════════════════════════════════════════════════════════════════════════════

# A SEPARATE router, mounted WITHOUT the session-auth dependency (Mailjet's callback
# carries no login cookie). Guarded instead by a shared token in the URL query. Same
# /outreach prefix so it's covered by the existing nginx proxy allowlist.
webhook_router = APIRouter(prefix="/outreach", tags=["Outreach"])


@webhook_router.post("/mailjet-events", status_code=status.HTTP_200_OK)
async def mailjet_events(
    payload: list[dict] | dict | None = Body(default=None),
    token: str | None = Query(default=None, description="Shared webhook secret (MAILJET_WEBHOOK_TOKEN)."),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Receive Mailjet event callbacks (sent/open/click/bounce/blocked/spam) and
    advance the matching outreach row's delivery engagement. Mailjet posts either a
    single event object or a batch array; both are handled. Always returns 200 so
    Mailjet doesn't retry on a benign no-match."""
    settings = get_settings()
    expected = (settings.mailjet_webhook_token or "").strip()
    if expected and (token or "") != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook token")
    events = payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) else []
    updated = await record_delivery_events(db, events)
    logger.info("mailjet_events_received", count=len(events), updated=updated)
    return {"received": len(events), "updated": updated}
