"""Manual auto-outreach sweep.

Optional third phase of the manual maintenance workflow
(scripts/reenrich_and_apollo.py): after run_reenrichment_sweep() re-extracts
degraded jobs and run_apollo_recruiter_sweep() backfills recruiter emails, this
sends the initial recruiter outreach for every harvested job that now has a
resolvable email but hasn't been contacted yet — so the freshly-enriched
recruiters actually get emailed instead of only sitting in the DB.

It adds NO new send logic: it selects the eligible job rows and hands them to the
same run_auto_outreach_after_harvest() the end-of-harvest flow uses
(app/routes/run_harvest_agent.py). That means every existing guard still applies:
  * gate         — settings.outreach_auto_send_on_harvest (a no-op when OFF)
  * dedup        — one send per recruiter (auto_outreach_service._dedupe_targets)
  * idempotency  — outreach_log_service.initial_email_sent (never re-contacts)
  * do-not-contact — suppression_service.is_suppressed + RecruiterORM.unsubscribed
  * identity/rotation — the configured OUTREACH_AUTO_REPLY_TO handling

Because the harvest daily cap never gates it (it never touches run_guard), this can
drain the never-contacted backlog the same way the reenrichment/Apollo sweeps drain
theirs. Kept separate from the harvest flow so it runs standalone. Never raises — a
failure here must not abort the maintenance workflow.
"""
from __future__ import annotations

import structlog
from sqlalchemy import and_, or_, select

from app.config import get_settings
from app.models.harvest_run import ScrapedJobORM
from app.models.recruiter import RecruiterORM
from app.services.auto_outreach_service import run_auto_outreach_after_harvest
from app.services.harvest_run_service import db_read

logger = structlog.get_logger(__name__)

# Synthetic run id used only for this sweep's structured logs — auto-outreach reads
# run_id purely for logging (it does no per-run DB lookup on it).
_SWEEP_RUN_ID = "manual-outreach-sweep"


async def _list_emailable_jobs(db, limit: int) -> list[ScrapedJobORM]:
    """Job rows eligible for outreach: those whose OWN scraped email is set, or whose
    linked recruiter now has an email (the enriched contact scraped_job_view merges in).
    Outer join so a job with a scraped email but no recruiter still qualifies. Newest
    posting first, so _dedupe_targets keeps the most recent job per recruiter. The
    recruiter relationship auto-loads (lazy="selectin"), which auto-outreach needs for
    the unsubscribed flag and the merged email."""
    job_has_email = and_(ScrapedJobORM.email_id.isnot(None), ScrapedJobORM.email_id != "")
    recruiter_has_email = and_(
        RecruiterORM.official_email_id.isnot(None), RecruiterORM.official_email_id != ""
    )
    stmt = (
        select(ScrapedJobORM)
        .outerjoin(RecruiterORM, ScrapedJobORM.recruiter_id == RecruiterORM.id)
        .where(or_(job_has_email, recruiter_has_email))
        .order_by(ScrapedJobORM.posted_date.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars())


async def run_outreach_sweep(limit: int | None = None) -> dict:
    """Send initial outreach to every not-yet-contacted recruiter with a resolvable
    email, reusing the end-of-harvest auto-outreach flow.

    Returns auto-outreach's summary dict (enabled/eligible + per-outcome counts). Add
    a `candidates` key: how many job rows were selected before dedup. `limit` defaults
    to a very large value (drain the whole eligible backlog); pass a smaller number to
    cap sends. Never raises."""
    lim = limit if limit is not None else 1_000_000
    settings = get_settings()

    # Cheap early exit + a clear reason: the send itself is gated inside
    # run_auto_outreach_after_harvest, but surfacing it here avoids a confusing
    # "0 sent" with no explanation when the global toggle is off.
    if not settings.outreach_auto_send_on_harvest:
        logger.info("outreach_sweep_disabled")
        return {"enabled": False, "candidates": 0}

    rows = await db_read(lambda db: _list_emailable_jobs(db, lim)) or []
    if not rows:
        logger.info("outreach_sweep_no_candidates")
        return {"enabled": True, "candidates": 0, "eligible": 0}

    logger.debug("outreach_sweep_start", candidates=len(rows))
    summary = await run_auto_outreach_after_harvest(rows, run_id=_SWEEP_RUN_ID)
    summary["candidates"] = len(rows)
    logger.info("outreach_sweep_done", **summary)
    return summary
