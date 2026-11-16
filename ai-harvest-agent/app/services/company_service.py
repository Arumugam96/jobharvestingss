"""Company-level enrichment cache (CompanyORM) — key + read + upsert + cooldown.

Why this exists
───────────────
The per-recruiter Apollo people-match (app/agents/linkedin_agent._enrich_recruiters)
returns a company's size / HQ location for free — but ONLY for jobs whose poster
Apollo matched. Jobs with no recruiter got no company size/location.

The company-enrichment pass (LinkedInAgent._enrich_companies / _run_company_waterfall:
Apollo-by-domain → LinkedIn company card via BeautifulSoup + LLM) fills that gap and
caches the result here, once per unique company. HarvestRunService.bulk_insert_scraped_jobs
then fills EVERY job of that company from this cache, recruiter or not.

This module owns only the cache mechanics — the normalized dedup key, batch read,
upsert (non-empty-wins merge), and the enrich-once predicate. The actual
Apollo/LinkedIn fetching lives in the agent.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.job_url import normalize_company_key  # noqa: F401 — re-exported
from app.models.harvest_run import CompanyORM


async def get_companies_by_keys(db: AsyncSession, keys: set[str]) -> dict[str, CompanyORM]:
    """Fetch cached CompanyORM rows for a set of company keys, mapped by key."""
    keys = {k for k in keys if k}
    if not keys:
        return {}
    result = await db.execute(select(CompanyORM).where(CompanyORM.company_key.in_(keys)))
    return {c.company_key: c for c in result.scalars()}


async def _get_or_create_company(db: AsyncSession, company_key: str, company_name: str) -> CompanyORM:
    """The cache row for ``company_key``, inserted when missing (race-safe: a
    concurrent insert of the same key is re-read instead of failing)."""
    result = await db.execute(select(CompanyORM).where(CompanyORM.company_key == company_key))
    row = result.scalar_one_or_none()
    if row is None:
        row = CompanyORM(id=str(uuid.uuid4()), company_key=company_key, company_name=company_name)
        try:
            async with db.begin_nested():
                db.add(row)
                await db.flush()
        except IntegrityError:
            row = (await db.execute(
                select(CompanyORM).where(CompanyORM.company_key == company_key)
            )).scalar_one()
    return row


async def record_apollo_org(
    db: AsyncSession,
    *,
    company_key: str,
    company_name: str,
    org_id: str,
    domain: str = "",
    company_size: str = "",
    company_country: str = "",
    company_state: str = "",
    company_industry: str = "",
) -> None:
    """Cache one Apollo organization resolve attempt — ``org_id`` "" records a miss.
    Always stamps apollo_org_resolved_at (a miss is never retried); the org's details ride
    along for free and fill the same non-empty-wins fields as the company pass. Does
    NOT touch apollo_enriched_at — that is the company pass's own cooldown clock."""
    row = await _get_or_create_company(db, company_key, company_name)
    for field, value in (
        ("apollo_org_id", org_id), ("domain", domain), ("company_size", company_size),
        ("company_country", company_country), ("company_state", company_state),
        ("company_industry", company_industry),
    ):
        if value:
            setattr(row, field, value)
    row.apollo_org_resolved_at = datetime.now(timezone.utc)
    await db.flush()


def apollo_org_resolve_due(row: CompanyORM | None) -> bool:
    """A paid Apollo org lookup is due only for a company never resolved before — a
    cached org id is reused and a cached miss is never retried (no re-sweeps of old
    data; only new companies are looked up)."""
    if row is None:
        return True
    return not row.apollo_org_id and row.apollo_org_resolved_at is None


async def upsert_company_enrichment(
    db: AsyncSession,
    *,
    company_key: str,
    company_name: str,
    domain: str,
    company_size: str,
    company_country: str,
    company_state: str,
    company_industry: str,
    apollo_attempted: bool,
) -> None:
    """Insert-or-update one company cache row. Only overwrites a field with a
    non-empty value (never erases a previous hit with a later miss)."""
    now = datetime.now(timezone.utc)
    row = await _get_or_create_company(db, company_key, company_name)

    def _set(field: str, value: str) -> None:
        """Set ``field`` on the company row only when ``value`` is non-empty."""
        if value:
            setattr(row, field, value)

    _set("company_name", company_name)
    _set("domain", domain)
    _set("company_size", company_size)
    _set("company_country", company_country)
    _set("company_state", company_state)
    _set("company_industry", company_industry)
    if apollo_attempted:
        row.apollo_attempted = True
    # Stamp every enrichment attempt (any stage, not just Apollo) so
    # company_needs_enrichment never re-runs it, even when nothing was found.
    row.apollo_enriched_at = now
    await db.flush()


def company_needs_enrichment(row: CompanyORM | None) -> bool:
    """A company needs enrichment only if it has never been through the company pass
    (no apollo_enriched_at stamp) and doesn't already hold BOTH a size and an HQ
    country. A company already attempted is never re-enriched, even when the attempt
    filled only one field or nothing — no re-sweeps of old data."""
    if row is not None and row.company_size and row.company_country:
        return False  # already have both — nothing more to fetch
    return row is None or row.apollo_enriched_at is None
