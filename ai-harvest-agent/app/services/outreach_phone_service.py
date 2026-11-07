"""Apollo phone request AFTER a successful initial outreach email.

Phones cost 8 Apollo credits each, so they are requested only for contacts we are
actively working: right after an initial outreach email was actually SENT (auto
end-of-harvest outreach — auto_outreach_service — and the manual composer —
POST /send-email), and only when ALL hold:

  1. the recipient is the job's poster / recruiter (a RecruiterORM row), not a
     company HR shortlist contact;
  2. that recruiter has a LinkedIn profile URL;
  3. the company size is KNOWN (scraped_jobs.company_size — which already carries the
     Apollo org band merged at insert time — else recruiters.company_size);
  4. that band is one of the Rule Engine size pills (harvest_config
     ``filters.company_sizes``) — band MEMBERSHIP, not the outreach minimum floor; an
     empty selection means no size restriction;
  5. no phone yet: recruiters.contact_number is empty and there is no pending / found
     apollo_phone_requests row for the recruiter or its Apollo person id — and the
     enrichment budget covers it on top of every outstanding enrichment phone.

Gated by settings.apollo_reveal_phone + a configured webhook URL. The request row
(purpose "enrichment", source "outreach") is created before the Apollo call and its
ref rides in the webhook URL, so the async delivery lands on the recruiter
(apollo_phone_service). Never raises: a failure here must not touch the send.
"""
from __future__ import annotations

import asyncio
from typing import Any

import structlog
from sqlalchemy import or_, select

from app.config import get_settings
from app.core.company_size import normalize_company_size
from app.models.apollo_phone import ApolloPhoneRequestORM
from app.models.harvest_run import ScrapedJobORM
from app.models.recruiter import RecruiterORM
from app.services import apollo_budget, apollo_phone_service
from app.services.harvest_run_service import db_read, db_write

logger = structlog.get_logger(__name__)

# Skip reasons (logged as `reason` on apollo_outreach_phone_skipped).
DISABLED = "disabled"
NO_LINKEDIN = "no_linkedin"
SIZE_UNKNOWN = "size_unknown"
SIZE_NOT_IN_FILTER = "size_not_in_filter"
ALREADY_HAS_PHONE = "already_has_phone"
BUDGET = "budget"
REQUESTED = "requested"
ERROR = "error"

_background: set[asyncio.Task] = set()


def _clean(v: Any) -> str:
    return str(v or "").strip()


def size_band(*values: str) -> str:
    """Canonical band token of the first value that carries a recognisable band
    ("" = unknown). NOT_FOUND / "" / free text without a range → unknown."""
    for v in values:
        tok = normalize_company_size(_clean(v))
        if tok:
            return tok
    return ""


def size_decision(band: str, filter_sizes: list[str] | None) -> str | None:
    """None when the size passes, else the skip reason. Unknown size never passes;
    an empty filter selection means any KNOWN size passes."""
    if not band:
        return SIZE_UNKNOWN
    wanted = {t for s in (filter_sizes or []) if (t := normalize_company_size(_clean(s)))}
    if wanted and band not in wanted:
        return SIZE_NOT_IN_FILTER
    return None


def resolve_filter_sizes(company_sizes: list[str] | None) -> list[str]:
    """The Rule Engine size pills: ``company_sizes`` when given, else harvest_config."""
    if company_sizes is not None:
        return list(company_sizes)
    try:
        from app.services.config_service import ConfigService

        return list(ConfigService().load().filters.company_sizes or [])
    except Exception as exc:  # unreadable config → no size restriction (rules 1–3 still apply)
        logger.warning("apollo_outreach_phone_config_failed", error=str(exc))
        return []


async def _context(recruiter_id: str, job_id: str | None) -> dict | None:
    async def _q(db) -> dict | None:
        rec = await db.get(RecruiterORM, recruiter_id)
        if rec is None:
            return None
        job = await db.get(ScrapedJobORM, job_id) if job_id else None
        R = ApolloPhoneRequestORM
        owned = [R.recruiter_id == recruiter_id]
        if rec.apollo_person_id:
            owned.append(R.apollo_person_id == rec.apollo_person_id)
        open_rows = (await db.execute(
            select(R.id).where(or_(*owned), R.status.in_((apollo_phone_service.PENDING,
                                                           apollo_phone_service.FOUND))).limit(1)
        )).first()
        return {
            "tenant_id": rec.tenant_id or "internal",
            "name": rec.person_name or "", "company": rec.company_name or "",
            "linkedin_url": _clean(rec.linkedin_profile_url) or _clean(getattr(job, "linkedin_profile_url", "")),
            "person_id": _clean(rec.apollo_person_id),
            "contact_number": _clean(rec.contact_number),
            "recruiter_email": _clean(rec.official_email_id).lower(),
            "job_email": _clean(getattr(job, "email_id", "")).lower(),
            "band": size_band(getattr(job, "company_size", ""), rec.company_size),
            "has_open_request": open_rows is not None,
        }
    return await db_read(_q)


def _skip(reason: str, **kw) -> str:
    logger.info("apollo_outreach_phone_skipped", reason=reason, **kw)
    return reason


async def request_phone_after_outreach(
    *, recruiter_id: str | None, job_id: str | None = None, to_email: str = "",
    company_sizes: list[str] | None = None, client: Any = None,
) -> str:
    """Request the recruiter's phone from Apollo after an initial outreach email was
    sent to them. Returns REQUESTED or a skip reason / ERROR. Never raises."""
    try:
        return await _request(recruiter_id=recruiter_id, job_id=job_id, to_email=to_email,
                              company_sizes=company_sizes, client=client)
    except Exception as exc:
        logger.warning("apollo_outreach_phone_failed", recruiter_id=recruiter_id, job_id=job_id,
                       error=str(exc))
        return ERROR


async def _request(
    *, recruiter_id: str | None, job_id: str | None, to_email: str,
    company_sizes: list[str] | None, client: Any,
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
    if not recruiter_id:
        return _skip(NO_LINKEDIN, detail="no_recruiter", **log)

    ctx = await _context(recruiter_id, job_id)
    if ctx is None:
        return _skip(NO_LINKEDIN, detail="recruiter_not_found", **log)
    to = _clean(to_email).lower()
    if to and to not in {ctx["recruiter_email"], ctx["job_email"]} - {""}:
        # Sent to someone other than the poster/recruiter (e.g. the company HR contact).
        return _skip(NO_LINKEDIN, detail="recipient_not_recruiter", **log)
    if not ctx["linkedin_url"]:
        return _skip(NO_LINKEDIN, **log)
    reason = size_decision(ctx["band"], resolve_filter_sizes(company_sizes))
    if reason:
        return _skip(reason, band=ctx["band"], **log)
    if ctx["contact_number"] or ctx["has_open_request"]:
        return _skip(ALREADY_HAS_PHONE, **log)

    cost = apollo_phone_service.phone_cost()
    remaining = await apollo_budget.remaining_today(purpose="enrichment")
    if remaining is not None:
        outstanding = await apollo_phone_service.pending_phone_credits_for_purpose("enrichment")
        if remaining - outstanding < cost:
            return _skip(BUDGET, remaining=remaining, outstanding=outstanding, cost=cost, **log)

    ref = await apollo_phone_service.begin_request(
        tenant_id=ctx["tenant_id"], recruiter_id=recruiter_id, apollo_person_id=ctx["person_id"] or None,
        purpose="enrichment", source="outreach", linkedin_url=ctx["linkedin_url"],
    )
    if not ref:
        logger.warning("apollo_outreach_phone_row_failed", **log)
        return ERROR

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
        if pid:
            rec = await db.get(RecruiterORM, recruiter_id)
            if rec is not None and not rec.apollo_person_id:
                rec.apollo_person_id = pid[:60]
        return ps
    ps = await db_write(_settle) or ""
    logger.info("apollo_outreach_phone_requested", ref=ref, phone_status=ps, band=ctx["band"], **log)
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
