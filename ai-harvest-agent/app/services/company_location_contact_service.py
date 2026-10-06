"""Company + job-location HR-contact discovery + cache.

When a harvested job has NO resolvable recruiter/poster email, we still want to reach
someone at that company — but a company can post from multiple locations with different
recruiters, so the contact is discovered and cached by **(company, job-location)**,
never by company alone, and the Apollo people search is ALWAYS location-constrained with
no company-wide fallback.

This module owns:
  * the CompanyLocationContactORM cache (find/upsert + the recheck-cooldown predicate), and
  * ``discover_company_location_contacts`` — a best-effort, browser-free post-harvest pass
    (called from LinkedInAgent._run alongside _enrich_recruiters/_enrich_companies) that
    finds the email-less jobs, groups them by (company_key, location_key), and for each
    group that needs it runs ``apollo_company_contact_fallback`` and writes the result to
    the cache.

The pass writes ONLY the cache. The contact is attached to each email-less job at insert
time in HarvestRunService.bulk_insert_scraped_jobs (looked up by the same keys), exactly
like CompanyORM feeds company size/HQ — so the original LinkedIn poster fields are never
touched and nothing is plumbed through the job dataclasses.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.company_domain import infer_company_domain
from app.core.dependencies import get_session_factory
from app.core.location import location_search_terms, normalize_location_key
from app.models.harvest_run import CompanyLocationContactORM
from app.services import apollo_budget, run_guard
from app.services.apollo_enrichment import apollo_company_contact_fallback
from app.services.company_service import get_companies_by_keys, normalize_company_key
from app.services.recruiter_service import (
    compute_name_company_key,
    official_emails_by_name_company_keys,
)

logger = structlog.get_logger(__name__)

# Max distinct (company, location) pairs to Apollo-search per run — bounds search
# volume/time the same way _COMPANY_ENRICH_CAP bounds the company size pass. The daily
# credit cap (apollo_budget) is the hard account-wide backstop on top of this.
_COMPANY_CONTACT_CAP = 40


# ── Cache CRUD ──────────────────────────────────────────────────────────────────

async def get_location_contacts(
    db: AsyncSession, pairs: set[tuple[str, str]]
) -> dict[tuple[str, str], CompanyLocationContactORM]:
    """Fetch cached rows for a set of (company_key, location_key) pairs, mapped by the
    pair. Queries by the bounded set of company_keys and filters location_key in Python
    (portable, avoids composite-IN quirks across SQLite/Postgres)."""
    pairs = {p for p in pairs if p and p[0] and p[1]}
    if not pairs:
        return {}
    company_keys = {c for c, _ in pairs}
    result = await db.execute(
        select(CompanyLocationContactORM).where(
            CompanyLocationContactORM.company_key.in_(company_keys)
        )
    )
    out: dict[tuple[str, str], CompanyLocationContactORM] = {}
    for row in result.scalars():
        key = (row.company_key, row.location_key)
        if key in pairs:
            out[key] = row
    return out


async def upsert_location_contact(
    db: AsyncSession,
    *,
    company_key: str,
    location_key: str,
    company_name: str,
    location: str,
    domain: str,
    hr_contact_name: str,
    hr_contact_title: str,
    hr_contact_email: str,
    hr_contact_phone: str,
    attempted: bool,
) -> None:
    """Insert-or-update one (company, location) contact cache row. Non-empty-wins merge
    (never erases a previous hit with a later miss); stamps hr_contact_attempted_at on
    every real attempt so the recheck cooldown can gate re-searches of negatives too."""
    now = datetime.now(timezone.utc)
    row = (await db.execute(
        select(CompanyLocationContactORM).where(
            CompanyLocationContactORM.company_key == company_key,
            CompanyLocationContactORM.location_key == location_key,
        )
    )).scalar_one_or_none()
    if row is None:
        row = CompanyLocationContactORM(
            id=str(uuid.uuid4()), company_key=company_key, location_key=location_key,
            company_name=company_name, location=location,
        )
        try:
            async with db.begin_nested():
                db.add(row)
                await db.flush()
        except IntegrityError:
            row = (await db.execute(
                select(CompanyLocationContactORM).where(
                    CompanyLocationContactORM.company_key == company_key,
                    CompanyLocationContactORM.location_key == location_key,
                )
            )).scalar_one()

    def _set(field: str, value: str) -> None:
        if value:
            setattr(row, field, value)

    _set("company_name", company_name)
    _set("location", location)
    _set("domain", domain)
    _set("hr_contact_name", hr_contact_name)
    _set("hr_contact_title", hr_contact_title)
    _set("hr_contact_email", hr_contact_email)
    _set("hr_contact_phone", hr_contact_phone)
    if attempted:
        row.apollo_attempted = True
        row.hr_contact_attempted_at = now
    await db.flush()


def location_contact_needs_search(
    row: CompanyLocationContactORM | None, recheck_days: int
) -> bool:
    """A (company, location) needs an Apollo search unless it already holds a cached HR
    email, or it was searched within the recheck cooldown (so recent negatives aren't
    re-billed). A row never attempted (or with no stamp) is always eligible."""
    if row is not None and row.hr_contact_email:
        return False  # already have a usable contact — reuse it
    if row is None or row.hr_contact_attempted_at is None:
        return True
    last = row.hr_contact_attempted_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last) >= timedelta(days=recheck_days)


# ── Discovery pass ──────────────────────────────────────────────────────────────

def _job_attr(job, name: str) -> str:
    return (getattr(job, name, "") or "").strip()


async def discover_company_location_contacts(jobs: list, settings: Settings) -> dict:
    """Best-effort post-harvest pass: for every email-less harvested job, discover a
    location-specific HR contact for its (company, job-location) via Apollo and cache it.

    Writes ONLY the CompanyLocationContactORM cache — the contact is attached to jobs at
    insert time by bulk_insert_scraped_jobs. Never raises; returns a counts summary.
    """
    counts = {
        "searched": 0, "found": 0, "reused": 0,
        "skipped_no_location": 0, "skipped_cooldown": 0,
        "cap_deferred": 0, "pairs": 0,
    }
    if not settings.apollo_api_key or not settings.company_contact_fallback:
        return {"enabled": False, **counts}

    try:
        session_factory = get_session_factory(settings)

        # 1. Which posters already have an email (so their jobs are NOT candidates)?
        poster_keys: set[str] = set()
        for j in jobs:
            name = _job_attr(j, "job_poster_name")
            if name:
                company = _job_attr(j, "job_poster_company") or _job_attr(j, "company")
                poster_keys.add(compute_name_company_key(name, company))
        emails_have: set[str] = set()
        if poster_keys:
            async with session_factory() as db:
                emails_have = await official_emails_by_name_company_keys(db, poster_keys)

        # 2. Group the email-less jobs by (company_key, location_key).
        groups: dict[tuple[str, str], list] = {}
        for j in jobs:
            if _job_attr(j, "job_poster_email"):
                continue  # already carries a direct email
            name = _job_attr(j, "job_poster_name")
            if name:
                company = _job_attr(j, "job_poster_company") or _job_attr(j, "company")
                if compute_name_company_key(name, company) in emails_have:
                    continue  # poster's recruiter already has an email
            company_name = _job_attr(j, "company")
            if not company_name:
                continue
            lkey = normalize_location_key(_job_attr(j, "location"))
            if not lkey:
                counts["skipped_no_location"] += 1   # no derivable location → never company-wide
                continue
            ckey = normalize_company_key(company_name)
            if not ckey:
                continue
            groups.setdefault((ckey, lkey), []).append(j)

        counts["pairs"] = len(groups)
        if not groups:
            return {"enabled": True, **counts}

        # 3. Load the cache + any known company domains for better org resolution.
        async with session_factory() as db:
            cache = await get_location_contacts(db, set(groups.keys()))
            company_rows = await get_companies_by_keys(db, {ck for ck, _ in groups.keys()})

        # 4. Search each pair that needs it, largest groups first (max coverage/credit).
        ordered = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)
        searched = 0
        for (ckey, lkey), group_jobs in ordered:
            if searched >= _COMPANY_CONTACT_CAP:
                logger.info("company_location_contact_cap_reached", cap=_COMPANY_CONTACT_CAP)
                break
            if run_guard.is_stop_requested():
                logger.info("company_location_contact_stopped_by_user", searched=searched)
                break

            row = cache.get((ckey, lkey))
            if row is not None and row.hr_contact_email:
                counts["reused"] += 1   # already cached → attached at insert time
                continue
            if not location_contact_needs_search(row, settings.apollo_recheck_days):
                counts["skipped_cooldown"] += 1
                continue

            # Out of daily budget — defer this and the rest to the next day (cache nothing).
            remaining = await apollo_budget.remaining_today()
            if remaining is not None and remaining <= 0:
                counts["cap_deferred"] += 1
                logger.info("company_location_contact_budget_exhausted", searched=searched)
                break

            sample = group_jobs[0]
            company_name = _job_attr(sample, "company")
            location_raw = _job_attr(sample, "location")
            loc_terms = location_search_terms(location_raw)
            comp_row = company_rows.get(ckey)
            domain = (comp_row.domain if comp_row else "") or infer_company_domain(company_name)[0]

            result = await apollo_company_contact_fallback(
                settings=settings,
                company_name=company_name,
                location_terms=loc_terms,
                domain=domain,
                attempted_at=(row.hr_contact_attempted_at if row else None),
            )
            if result.cap_reached:
                counts["cap_deferred"] += 1
                logger.info("company_location_contact_cap_deferred", company=company_name)
                break  # budget gone — stop; retry next day, no negative cached

            searched += 1
            counts["searched"] += 1
            if result.email:
                counts["found"] += 1

            try:
                async with session_factory() as db:
                    await upsert_location_contact(
                        db,
                        company_key=ckey, location_key=lkey,
                        company_name=company_name, location=location_raw,
                        domain=result.company_domain or domain,
                        hr_contact_name=result.name, hr_contact_title=result.title,
                        hr_contact_email=result.email, hr_contact_phone=result.phone,
                        attempted=result.attempted,
                    )
                    await db.commit()
            except Exception as exc:
                logger.warning("company_location_contact_cache_write_failed", company=company_name, error=str(exc))

        logger.info("company_location_contact_pass_complete", **counts)
        return {"enabled": True, **counts}
    except Exception as exc:
        logger.warning("company_location_contact_discovery_failed", error=str(exc))
        return {"error": str(exc), **counts}
