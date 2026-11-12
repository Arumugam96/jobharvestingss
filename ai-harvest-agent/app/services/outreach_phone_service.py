"""Apollo phone request AFTER a successful initial outreach email.

Phones cost 8 Apollo credits each, so they are requested only for contacts we are
actively working: right after an initial outreach email was actually SENT (auto
end-of-harvest outreach — auto_outreach_service — and the manual composer —
POST /send-email), and only when ALL hold:

  1. the job or its recruiter has a LinkedIn profile URL;
  2. no phone yet: the job's contact_number is empty (and the recruiter's, when one
     exists), and there is no pending / found apollo_phone_requests row — and the
     enrichment budget covers it on top of every outstanding enrichment phone.

Company size is NOT checked here — the outreach decision already passed that filter.
A recruiter row is NOT required — when absent the job's own linkedin_profile_url is
used and the delivered phone is written to scraped_jobs.contact_number.

Gated by settings.apollo_reveal_phone + a configured webhook URL. The request row
(purpose "enrichment", source "outreach") is created before the Apollo call and its
ref rides in the webhook URL, so the async delivery lands on the recruiter
(apollo_phone_service). Never raises: a failure here must not touch the send.

A 5-second delay is applied before the Apollo call to avoid bursting rate limits.
"""
from __future__ import annotations

import asyncio
from typing import Any

import structlog
from sqlalchemy import or_, select

from app.config import get_settings
from app.models.apollo_phone import ApolloPhoneRequestORM
from app.models.harvest_run import ScrapedJobORM
from app.models.recruiter import RecruiterORM
from app.services import apollo_budget, apollo_phone_service
from app.services.harvest_run_service import db_read, db_write

logger = structlog.get_logger(__name__)

# Skip reasons (logged as `reason` on apollo_outreach_phone_skipped).
DISABLED = "disabled"
NO_LINKEDIN = "no_linkedin"
ALREADY_HAS_PHONE = "already_has_phone"
BUDGET = "budget"
REQUESTED = "requested"
ERROR = "error"

_APOLLO_REQUEST_DELAY = 5  # seconds between each Apollo phone request

_background: set[asyncio.Task] = set()


def _clean(v: Any) -> str:
    return str(v or "").strip()


async def _context(recruiter_id: str | None, job_id: str | None) -> dict | None:
    async def _q(db) -> dict | None:
        rec = await db.get(RecruiterORM, recruiter_id) if recruiter_id else None
        job = await db.get(ScrapedJobORM, job_id) if job_id else None
        if rec is None and job is None:
            return None

        linkedin_url = ""
        if rec:
            linkedin_url = _clean(rec.linkedin_profile_url)
        if not linkedin_url and job:
            linkedin_url = _clean(job.linkedin_profile_url)

        person_id = _clean(rec.apollo_person_id) if rec else ""

        R = ApolloPhoneRequestORM
        owned = []
        if recruiter_id:
            owned.append(R.recruiter_id == recruiter_id)
        if person_id:
            owned.append(R.apollo_person_id == person_id)
        if job_id:
            owned.append(R.scraped_job_id == job_id)
        if owned:
            open_rows = (await db.execute(
                select(R.id).where(or_(*owned), R.status.in_((apollo_phone_service.PENDING,
                                                               apollo_phone_service.FOUND))).limit(1)
            )).first()
        else:
            open_rows = None

        rec_phone = _clean(rec.contact_number) if rec else ""
        job_phone = _clean(job.contact_number) if job else ""

        return {
            "tenant_id": (rec.tenant_id if rec else job.tenant_id) or "internal",
            "name": (rec.person_name if rec else _clean(getattr(job, "job_poster_name", ""))) or "",
            "company": (rec.company_name if rec else _clean(getattr(job, "company", ""))) or "",
            "linkedin_url": linkedin_url,
            "person_id": person_id,
            "contact_number": rec_phone or job_phone,
            "recruiter_email": _clean(rec.official_email_id).lower() if rec else "",
            "job_email": _clean(job.email_id).lower() if job else "",
            "has_open_request": open_rows is not None,
            "job_id": job_id,
        }
    return await db_read(_q)


def _skip(reason: str, **kw) -> str:
    logger.info("apollo_outreach_phone_skipped", reason=reason, **kw)
    return reason


async def request_phone_after_outreach(
    *, recruiter_id: str | None, job_id: str | None = None, to_email: str = "",
    client: Any = None,
) -> str:
    """Request the recruiter's phone from Apollo after an initial outreach email was
    sent to them. Returns REQUESTED or a skip reason / ERROR. Never raises."""
    try:
        return await _request(recruiter_id=recruiter_id, job_id=job_id, to_email=to_email,
                              client=client)
    except Exception as exc:
        logger.warning("apollo_outreach_phone_failed", recruiter_id=recruiter_id, job_id=job_id,
                       error=str(exc))
        return ERROR


async def _request(
    *, recruiter_id: str | None, job_id: str | None, to_email: str,
    client: Any,
) -> str:
    settings = get_settings()
    log = {"recruiter_id": recruiter_id, "job_id": job_id}
    if not settings.apollo_reveal_phone or not settings.apollo_api_key:
        return _skip(DISABLED, **log)
    if client is None:
        from app.services.apollo_client import ApolloClient

        client = ApolloClient(settings, purpose="enrichment")
    if not getattr(client, "phone_enabled", False):
        return _skip(DISABLED, detail="no_webhook_url", **log)
    if not recruiter_id and not job_id:
        return _skip(NO_LINKEDIN, detail="no_recruiter_and_no_job", **log)

    ctx = await _context(recruiter_id, job_id)
    if ctx is None:
        return _skip(NO_LINKEDIN, detail="not_found", **log)
    if not ctx["linkedin_url"]:
        return _skip(NO_LINKEDIN, **log)
    if ctx["contact_number"] or ctx["has_open_request"]:
        return _skip(ALREADY_HAS_PHONE, **log)

    cost = apollo_phone_service.phone_cost()
    remaining = await apollo_budget.remaining_today(purpose="enrichment")
    if remaining is not None:
        outstanding = await apollo_phone_service.pending_phone_credits_for_purpose("enrichment")
        if remaining - outstanding < cost:
            return _skip(BUDGET, remaining=remaining, outstanding=outstanding, cost=cost, **log)

    ref = await apollo_phone_service.begin_request(
        tenant_id=ctx["tenant_id"], recruiter_id=recruiter_id,
        apollo_person_id=ctx["person_id"] or None,
        purpose="enrichment", source="outreach", linkedin_url=ctx["linkedin_url"],
        scraped_job_id=job_id,
    )
    if not ref:
        logger.warning("apollo_outreach_phone_row_failed", **log)
        return ERROR

    await asyncio.sleep(_APOLLO_REQUEST_DELAY)

    from app.services.apollo_client import ApolloAPIError

    try:
        if ctx["person_id"]:
            res = await client.match_person_by_id(
                ctx["person_id"], reveal_email=False, reveal_phone=True, webhook_ref=ref,
            )
        else:
            res = await client.enrich_person_by_linkedin(
                ctx["linkedin_url"], reveal_email=False, reveal_phone=True,
                name=ctx["name"], company=ctx["company"], webhook_ref=ref,
            )
    except ApolloAPIError as exc:
        logger.warning("apollo_outreach_phone_api_error", error=str(exc), **log)
        await db_write(lambda db: apollo_phone_service.settle_request(db, ref, None))
        return ERROR

    async def _settle(db) -> str:
        ps = await apollo_phone_service.settle_request(db, ref, res)
        pid = _clean(getattr(res, "id", "")) if getattr(res, "matched", False) else ""
        if pid and recruiter_id:
            rec = await db.get(RecruiterORM, recruiter_id)
            if rec is not None and not rec.apollo_person_id:
                rec.apollo_person_id = pid[:60]
        return ps
    ps = await db_write(_settle) or ""
    logger.info("apollo_outreach_phone_requested", ref=ref, phone_status=ps, **log)
    return REQUESTED if ps == apollo_phone_service.PS_PENDING else (ps.lower() or ERROR)


def schedule_phone_after_outreach(**kwargs) -> None:
    """Fire-and-forget ``request_phone_after_outreach`` (for the manual send route, so
    the Apollo round-trip never delays or fails the HTTP response)."""
    try:
        task = asyncio.get_running_loop().create_task(request_phone_after_outreach(**kwargs))
    except RuntimeError:  # no running loop — nothing to schedule on
        return
    _background.add(task)
    task.add_done_callback(_background.discard)
