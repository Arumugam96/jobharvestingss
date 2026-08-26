"""Recruiter outreach API — LLM-generated email + LinkedIn messages composed
from the Harvested Jobs table, sent over the existing SMTP transport.

Endpoints (Swagger-visible, session-authenticated like the other public routers):
  POST /outreach/generate-email     draft a recruiter email (tone + audience aware)
  POST /outreach/generate-linkedin  draft a LinkedIn outreach message
  POST /outreach/send-email         send a (possibly edited) email with the pptx attached

Generation is audited in the shared `llm_calls` table (call_type email_generation /
linkedin_generation, run_id NULL). Sends are logged in `email_outreach`.
"""
from __future__ import annotations

import re

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user, get_db_session, get_email_sender, get_llm_service
from app.models.auth import AuthenticatedUser
from app.models.harvest_run import LlmCallType
from app.models.outreach import EmailOutreachORM
from app.services.active_clients import classify_client
from app.services.email_service import EmailSender
from app.services.harvest_run_service import HarvestRunService, insert_llm_call, scraped_job_view
from app.services.llm_service import LLMService
from app.services.outreach_service import ATTACHMENT_NAME, ATTACHMENT_PATH, OutreachService
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

    draft = await OutreachService(llm_service).generate_email(
        view, client_type, tone, regenerate=body.regenerate, sender_email=current_user.email
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
        "attachment_name": ATTACHMENT_NAME,
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
    attached, using the editable From (Reply-To set to it; envelope stays the
    authenticated mailbox). Always returns 200 with a `status` so the failed row
    is persisted (raising would roll back the session)."""
    to_email = (body.to_email or "").strip()
    from_email = (body.from_email or "").strip()
    if not _EMAIL_RE.match(to_email):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid recipient email")
    if from_email and not _EMAIL_RE.match(from_email):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid sender email")
    if not (body.subject or "").strip() or not (body.body or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Subject and body are required")

    # Recompute company / recruiter link / audience from the job when available.
    company, recruiter_id, client_type = "", None, body.client_type or "unknown"
    if body.job_id:
        job = await HarvestRunService(db).get_scraped_job_by_id(body.job_id)
        if job is not None:
            company = job.company or ""
            recruiter_id = job.recruiter_id
            client_type = classify_client(company)

    attachments = [str(ATTACHMENT_PATH)] if ATTACHMENT_PATH.exists() else None
    if attachments is None:
        logger.warning("outreach_attachment_missing", path=str(ATTACHMENT_PATH))

    send_status, error_message = "sent", None
    try:
        await email_sender.send_email_with_attachments(
            recipients=[to_email],
            subject=body.subject,
            body=body.body,
            attachment_paths=attachments,
            from_email=from_email or None,
            reply_to=from_email or None,
            as_html=True,
        )
    except Exception as exc:  # SMTP/config failure — record and report, don't 500
        send_status, error_message = "failed", str(exc)
        logger.warning("outreach_send_failed", to=to_email, error=str(exc))

    db.add(EmailOutreachORM(
        job_id=body.job_id,
        recruiter_id=recruiter_id,
        company=company,
        client_type=client_type,
        tone=body.tone or "",
        to_email=to_email,
        from_email=from_email,
        subject=body.subject,
        body=body.body,
        attachment_name=ATTACHMENT_NAME if attachments else "",
        llm_generated=not body.fallback_used,
        fallback_used=body.fallback_used,
        status=send_status,
        error_message=error_message,
        sent_by=current_user.email,
    ))
    await db.flush()

    return {"status": send_status, "error": error_message}
