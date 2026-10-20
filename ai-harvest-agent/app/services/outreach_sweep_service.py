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

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import and_, exists, func, or_, select

from app.config import get_settings
from app.core.job_url import normalize_job_url
from app.core.tenant_context import apply_tenant, get_current_tenant_id, set_current_tenant
from app.models.harvest_run import ScrapedJobORM
from app.models.outreach import EmailOutreachORM
from app.models.recruiter import RecruiterORM
from app.models.resdex import ResdexSearchORM, ResdexSearchStatus
from app.services.auto_outreach_service import run_auto_outreach_after_harvest
from app.services.config_service import ConfigService
from app.services.harvest_run_service import _normalized_job_url, _txt_expr, db_read

logger = structlog.get_logger(__name__)

# Resdex outcomes that settle a job for RESDEX_REUSE_DAYS — the Apollo catch-up sweep
# never picks it again in that window. session_expired / session_busy / cap_reached /
# error stay retryable. A MATCHED job that was never emailed stays eligible while
# auto-send is on: its Apollo contact gets the card from the REUSED match (no new
# search); with auto-send off there is nothing left to do for it, so it is settled.
_SETTLED_RESDEX_STATUSES = (ResdexSearchStatus.NO_RESULTS, ResdexSearchStatus.SKIPPED_NO_JD)

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
    # Same minimum company-size floor the end-of-harvest send applies, from the
    # current Rule Engine config (empty ⇒ no gate).
    company_sizes = ConfigService().load().filters.company_sizes
    summary = await run_auto_outreach_after_harvest(
        rows, run_id=_SWEEP_RUN_ID, company_sizes=company_sizes,
    )
    summary["candidates"] = len(rows)
    logger.info("outreach_sweep_done", **summary)
    return summary


# ── Apollo-contact Resdex catch-up sweep ──────────────────────────────────────

async def _list_apollo_contact_jobs(
    db, *, sources: list[str], since: datetime, reuse_since: datetime, limit: int,
    matched_settles: bool = False,
) -> list[ScrapedJobORM]:
    """Recent LinkedIn/Naukri job rows (tenant-scoped) whose resolvable contact is
    Apollo-derived — a linked recruiter email Apollo revealed, or the company+location
    Apollo HR contact — with no email of their own, never sent an initial email
    (by job id or normalized posting URL) and with no settled Resdex search
    (no_results / skipped_no_jd, plus matched when ``matched_settles``) inside
    RESDEX_REUSE_DAYS. Newest first."""
    job_key = _normalized_job_url(ScrapedJobORM.job_url)
    apollo_recruiter = and_(
        _txt_expr(RecruiterORM.official_email_id) != "",
        func.lower(RecruiterORM.enrichment_source) == "apollo",
    )
    apollo_hr = _txt_expr(ScrapedJobORM.company_contact_email) != ""
    emailed = exists().where(
        EmailOutreachORM.channel == "email",
        EmailOutreachORM.outreach_kind == "initial",
        EmailOutreachORM.status == "sent",
        or_(
            EmailOutreachORM.job_id == ScrapedJobORM.id,
            and_(
                EmailOutreachORM.tenant_id == ScrapedJobORM.tenant_id,
                EmailOutreachORM.job_url == job_key,
            ),
        ),
    )
    settled = exists().where(
        ResdexSearchORM.tenant_id == ScrapedJobORM.tenant_id,
        ResdexSearchORM.job_url == job_key,
        ResdexSearchORM.status.in_(
            _SETTLED_RESDEX_STATUSES + ((ResdexSearchStatus.MATCHED,) if matched_settles else ())
        ),
        ResdexSearchORM.created_at >= reuse_since,
    )
    stmt = (
        apply_tenant(select(ScrapedJobORM), ScrapedJobORM.tenant_id)
        .outerjoin(RecruiterORM, ScrapedJobORM.recruiter_id == RecruiterORM.id)
        .where(
            _txt_expr(ScrapedJobORM.email_id) == "",
            or_(apollo_recruiter, apollo_hr),
            func.lower(ScrapedJobORM.source).in_([s.lower() for s in sources]),
            ScrapedJobORM.created_at >= since,
            ~emailed,
            ~settled,
        )
        .order_by(ScrapedJobORM.created_at.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars())


async def run_apollo_resdex_sweep(
    *, run_id: str, company_sizes: list[str] | None = None,
) -> dict:
    """Catch-up Resdex + outreach for jobs whose Apollo-enriched contact arrived after
    their own harvest (Contact Finder, prospect intelligence, the Apollo recruiter
    sweep, a later company-HR discovery).

    Called at the end of a harvest run (app/routes/run_harvest_agent.py), still inside
    that run's single-flight guard, so the recruiter browser profile is never shared
    with another run. Selects at most RESDEX_APOLLO_SWEEP_MAX_JOBS rows harvested in
    the last RESDEX_APOLLO_SWEEP_LOOKBACK_DAYS (see _list_apollo_contact_jobs) and
    hands them, one tenant at a time with that tenant bound, to the same
    run_auto_outreach_after_harvest — every send guard, the Resdex daily caps and the
    Resdex-only mode (auto-send off) apply unchanged. Never waits for a recruiter
    login (the harvest's own Resdex pass already did). Off unless RESDEX_ENABLED and
    RESDEX_INCLUDE_APOLLO_CONTACTS. Never raises — returns a summary dict."""
    summary: dict = {"enabled": False, "candidates": 0, "jobs": 0, "tenants": 0}
    try:
        settings = get_settings()
        max_jobs = int(getattr(settings, "resdex_apollo_sweep_max_jobs", 0) or 0)
        if not (settings.resdex_enabled
                and getattr(settings, "resdex_include_apollo_contacts", False)
                and max_jobs > 0):
            return summary
        summary["enabled"] = True

        from app.services import run_guard
        if run_guard.is_stop_requested():
            logger.info("apollo_resdex_sweep_stopped", run_id=run_id)
            return summary

        now = datetime.now(timezone.utc)
        lookback = max(1, int(getattr(settings, "resdex_apollo_sweep_lookback_days", 7) or 7))
        reuse_days = max(0, int(settings.resdex_reuse_days or 0))
        rows = await db_read(lambda db: _list_apollo_contact_jobs(
            db, sources=settings.resdex_source_list, since=now - timedelta(days=lookback),
            reuse_since=now - timedelta(days=reuse_days), limit=max_jobs * 3,
            matched_settles=not settings.outreach_auto_send_on_harvest,
        )) or []
        summary["candidates"] = len(rows)

        # One row per posting (a re-harvested posting has a row per run), grouped by
        # tenant in newest-first order.
        by_tenant: dict[str, list[ScrapedJobORM]] = {}
        seen: set[str] = set()
        for row in rows:
            key = normalize_job_url(row.job_url)
            if not key or key in seen:
                continue
            seen.add(key)
            by_tenant.setdefault(row.tenant_id or "internal", []).append(row)

        # Resdex searches and outreach rows are stamped with the CURRENT tenant, so
        # bind each job group's own tenant while it runs; restore afterwards.
        prev_tenant = get_current_tenant_id()
        results: dict[str, dict] = {}
        picked_total = 0
        try:
            for tid, tenant_rows in by_tenant.items():
                keep = tenant_rows[: max(0, max_jobs - picked_total)]
                if not keep:
                    break
                picked_total += len(keep)
                set_current_tenant(tid)
                results[tid] = await run_auto_outreach_after_harvest(
                    keep, run_id=f"{run_id}-apollo", company_sizes=company_sizes,
                    resdex_login_wait=False,
                )
        finally:
            set_current_tenant(prev_tenant)
        summary.update(jobs=picked_total, tenants=len(results), results=results)
        if not picked_total:
            logger.info("apollo_resdex_sweep_no_jobs", run_id=run_id, candidates=len(rows))
            return summary
        logger.info("apollo_resdex_sweep_done", run_id=run_id, jobs=picked_total,
                    tenants=len(results))
        return summary
    except Exception as exc:  # never let the sweep break the harvest completion path
        logger.warning("apollo_resdex_sweep_failed", run_id=run_id, error=str(exc))
        return {**summary, "error": str(exc)}
