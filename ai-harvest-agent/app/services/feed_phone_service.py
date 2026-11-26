"""Apollo phone requests for LinkedIn Feed leads, right after a feed run is saved.

A feed lead's poster is asked about (by their LinkedIn profile URL) only when ALL hold:

  1. the poster is a person (a linkedin.com/in/ URL on the job or its recruiter);
  2. there is NO email for them — neither in the post (scraped_jobs.email_id) nor on
     the linked recruiter row (official_email_id);
  3. the company size is KNOWN and its band starts above 500 employees
     (LINKEDIN_FEED_APOLLO_PHONE_MIN_EMPLOYEES, default 501 ⇒ "501-1,000" and up);
  4. no phone yet and no open Apollo phone request (checked by
     outreach_phone_service.request_phone, which also does the budget check).

Capped at LINKEDIN_FEED_APOLLO_PHONE_MAX requests per run, and gated by
LINKEDIN_FEED_APOLLO_PHONE plus APOLLO_REVEAL_PHONE and APOLLO_WEBHOOK_URL (without
those every request is skipped as "disabled"). Phones cost 8 credits each and arrive
asynchronously via /recruiter-finder/apollo-phone-webhook onto the recruiter / job
row. Never raises: a failure here must not affect the harvest run.
"""
from __future__ import annotations

import asyncio
from typing import Any

import structlog
from sqlalchemy import select

from app.config import get_settings
from app.core.company_size import band_lower_bound
from app.core.dependencies import get_session_factory
from app.core.tenant_context import bind_session_all_access
from app.models.harvest_run import ScrapedJobORM
from app.models.recruiter import RecruiterORM
from app.services import outreach_phone_service

logger = structlog.get_logger(__name__)

FEED_SOURCE = "LinkedIn Feed"
PHONE_SOURCE = "linkedin_feed"

_background: set[asyncio.Task] = set()


def _clean(v: Any) -> str:
    return str(v or "").strip()


def is_phone_candidate(
    *, linkedin_url: str, job_email: str, recruiter_email: str, job_phone: str,
    company_size: str, min_employees: int,
) -> bool:
    """The feed-lead phone rule (pure): a person's LinkedIn profile, no email
    anywhere, no phone yet, and a known company size whose band starts at or above
    ``min_employees``. An unknown size never qualifies."""
    if "linkedin.com/in/" not in linkedin_url.lower():
        return False
    if _clean(job_email) or _clean(recruiter_email) or _clean(job_phone):
        return False
    lower = band_lower_bound(company_size or "")
    return lower is not None and lower >= min_employees


async def _candidates(run_pk: str, min_employees: int) -> list[dict]:
    """The run's feed leads that pass is_phone_candidate, one per poster profile."""
    session_factory = get_session_factory(get_settings())
    async with session_factory() as db:
        await bind_session_all_access(db)  # leads may sit in several tenants
        rows = (await db.execute(
            select(ScrapedJobORM, RecruiterORM)
            .outerjoin(RecruiterORM, RecruiterORM.id == ScrapedJobORM.recruiter_id)
            .where(ScrapedJobORM.run_id == run_pk, ScrapedJobORM.source == FEED_SOURCE)
        )).all()

    out: list[dict] = []
    seen: set[str] = set()
    for job, rec in rows:
        linkedin_url = _clean(rec.linkedin_profile_url if rec else "") or _clean(job.linkedin_profile_url)
        if not is_phone_candidate(
            linkedin_url=linkedin_url,
            job_email=job.email_id or "",
            recruiter_email=(rec.official_email_id if rec else "") or "",
            job_phone=(job.contact_number or "") or ((rec.contact_number if rec else "") or ""),
            company_size=job.company_size or "",
            min_employees=min_employees,
        ):
            continue
        key = linkedin_url.lower().rstrip("/").split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append({"job_id": job.id, "recruiter_id": job.recruiter_id,
                    "company": job.company, "company_size": job.company_size})
    return out


async def request_feed_lead_phones(run_pk: str) -> dict:
    """Request Apollo phones for one saved feed run's qualifying leads. Returns a
    counts summary. Never raises."""
    settings = get_settings()
    summary: dict[str, Any] = {"candidates": 0, "requested": 0, "skipped": {}}
    if not settings.linkedin_feed_apollo_phone:
        return summary
    max_requests = max(0, int(settings.linkedin_feed_apollo_phone_max))
    min_employees = int(settings.linkedin_feed_apollo_phone_min_employees)
    try:
        cands = await _candidates(run_pk, min_employees)
    except Exception as exc:
        logger.warning("feed_phone_candidates_failed", run_pk=run_pk, error=str(exc))
        return summary
    summary["candidates"] = len(cands)

    for c in cands[:max_requests]:
        res = await outreach_phone_service.request_phone(
            recruiter_id=c["recruiter_id"], job_id=c["job_id"], source=PHONE_SOURCE,
        )
        if res == outreach_phone_service.REQUESTED:
            summary["requested"] += 1
        else:
            summary["skipped"][res] = summary["skipped"].get(res, 0) + 1
        if res in (outreach_phone_service.BUDGET, outreach_phone_service.DISABLED):
            break  # nothing further can succeed this run
    logger.info("feed_phone_requests_done", run_pk=run_pk, max_requests=max_requests,
                min_employees=min_employees, **summary)
    return summary


def schedule_feed_lead_phones(run_pk: str | None) -> None:
    """Fire-and-forget request_feed_lead_phones on the running loop (keeps a strong
    reference so the task isn't garbage-collected), so the ~5 s pause before each
    Apollo call never holds up the run's completion or the harvest single-flight slot."""
    if not run_pk or not get_settings().linkedin_feed_apollo_phone:
        return
    task = asyncio.get_running_loop().create_task(
        request_feed_lead_phones(run_pk), name=f"feed-phones-{run_pk}",
    )
    _background.add(task)
    task.add_done_callback(_background.discard)
