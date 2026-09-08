"""Manual Apollo recruiter sweep.

Second phase of the manual maintenance workflow (scripts/reenrich_and_apollo.py):
after run_reenrichment_sweep() re-extracts degraded jobs and upserts recruiter
identity, this takes every recruiter that now has a LinkedIn URL but still no
email and asks Apollo (by LinkedIn URL) for their contact, writing the result
back onto the recruiters table.

Reuses the exact pieces the harvest already uses — apollo_contact_fallback (the
one place credit-conservation lives: gate + recheck cooldown) and
save_enrichment — so there is no new Apollo logic here, only the batching. The
Apollo email/phone → save_enrichment field mapping mirrors the inline pass in
linkedin_agent._enrich_recruiters.

Kept separate from the harvest flow (like reenrichment_service) so it can be run
standalone regardless of the daily job cap. Never raises — a failure here must
not abort a batch or a caller.
"""
from __future__ import annotations

import structlog

from app.config import get_settings
from app.services.apollo_enrichment import apollo_contact_fallback
from app.services.harvest_run_service import db_read, db_write
from app.services.recruiter_service import list_recruiters_missing_email, save_enrichment

logger = structlog.get_logger(__name__)


async def run_apollo_recruiter_sweep(limit: int | None = None) -> dict[str, int]:
    """Apollo-enrich recruiters that have a LinkedIn URL but no email yet.

    Returns a summary dict {candidates, attempted, emails_found, skipped}:
      • candidates    — recruiters selected (missing email, has LinkedIn URL)
      • attempted     — Apollo calls actually issued (gate + cooldown passed)
      • emails_found  — of those, how many returned an email
      • skipped       — no-op'd by apollo_contact_fallback (unconfigured / cooldown)

    `limit` defaults to a very large value (drain all candidates); pass a smaller
    number to cap credit spend. Never raises."""
    settings = get_settings()
    lim = limit if limit is not None else 1_000_000
    summary = {"candidates": 0, "attempted": 0, "emails_found": 0, "skipped": 0}

    if not settings.apollo_api_key:
        logger.warning("apollo_recruiter_sweep_no_api_key")
        return summary

    try:
        rows = await db_read(lambda db: list_recruiters_missing_email(db, limit=lim)) or []
    except Exception as exc:  # defensive — db_read already swallows, but never raise upward
        logger.warning("apollo_recruiter_sweep_list_failed", error=str(exc))
        return summary

    if not rows:
        return summary
    summary["candidates"] = len(rows)
    logger.debug("apollo_recruiter_sweep_start", candidates=len(rows))

    for r in rows:
        logger.debug(
            "apollo_recruiter_task_started",
            recruiter_id=r["id"],
            person=r["person_name"],
            linkedin_url=r["linkedin_profile_url"],
        )
        try:
            res = await apollo_contact_fallback(
                settings=settings,
                linkedin_url=r["linkedin_profile_url"] or "",
                person_name=r["person_name"] or "",
                company_name=r["company_name"] or "",
                company_domain=r["company_domain"] or "",
                already_email=bool(r["official_email_id"]),
                already_phone=bool(r["contact_number"]),
                apollo_enriched_at=r["apollo_enriched_at"],
            )
        except Exception as exc:
            logger.warning("apollo_recruiter_task_error", recruiter_id=r["id"], error=str(exc))
            continue

        if not res.attempted:
            # Unconfigured, no URL, nothing left to reveal, or within the recheck
            # cooldown — apollo_contact_fallback declined to spend a credit.
            summary["skipped"] += 1
            continue
        summary["attempted"] += 1
        if res.email:
            summary["emails_found"] += 1

        # Persist the attempt (hit or miss). apollo_attempted stamps
        # apollo_enriched_at so the cooldown backs us off this profile next run,
        # even when Apollo returned nothing. Field mapping copies the inline pass
        # in linkedin_agent._enrich_recruiters (save_enrichment call).
        found_email = bool(res.email)
        found_phone = bool(res.phone)
        await db_write(lambda db, res=res, rid=r["id"], fe=found_email, fp=found_phone: save_enrichment(
            db, rid,
            official_email_id=res.email,
            email_status="PUBLIC" if fe else "NOT_FOUND",
            contact_number=res.phone,
            phone_status="PUBLIC" if fp else "NOT_FOUND",
            secondary_email=res.secondary_email,
            company_linkedin_url=res.company_linkedin_url,
            address=res.address,
            city=res.city,
            state=res.state,
            country=res.country,
            verified=fe or fp,
            enrichment_source=res.enrichment_source,
            apollo_attempted=res.attempted,
        ))

    logger.info("apollo_recruiter_sweep_done", **summary)
    return summary
