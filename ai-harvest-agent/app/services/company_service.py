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
upsert (non-empty-wins merge), and the recheck-cooldown predicate. The actual
Apollo/LinkedIn fetching lives in the agent.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.harvest_run import CompanyORM

_LEGAL_SUFFIXES = re.compile(
    r"\b(inc|incorporated|ltd|limited|llc|llp|pvt|private|corp|corporation|co|plc|gmbh|"
    r"technologies|technology|solutions|services|systems|labs|software)\b"
)
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def normalize_company_key(company_name: str) -> str:
    """Stable dedup key for a company name — lower-cased, legal/industry suffixes
    and punctuation stripped, whitespace collapsed. "" for a blank name."""
    text = (company_name or "").lower()
    text = _LEGAL_SUFFIXES.sub("", text)
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


async def get_companies_by_keys(db: AsyncSession, keys: set[str]) -> dict[str, CompanyORM]:
    """Fetch cached CompanyORM rows for a set of company keys, mapped by key."""
    keys = {k for k in keys if k}
    if not keys:
        return {}
    result = await db.execute(select(CompanyORM).where(CompanyORM.company_key.in_(keys)))
    return {c.company_key: c for c in result.scalars()}


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

    def _set(field: str, value: str) -> None:
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
    # company_needs_enrichment's cooldown gates re-runs even when nothing was found.
    row.apollo_enriched_at = now
    await db.flush()


def company_needs_enrichment(row: CompanyORM | None, recheck_days: int) -> bool:
    """A company needs (re)enrichment unless it already holds BOTH a size and an HQ
    country. A row with only one of them is retried once its cooldown elapses (the
    cooldown clock is apollo_enriched_at, set whenever Apollo was attempted); a row
    never attempted (or with no stamp) is always eligible."""
    if row is not None and row.company_size and row.company_country:
        return False  # already have both — nothing more to fetch
    if row is None or row.apollo_enriched_at is None:
        return True
    last = row.apollo_enriched_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last) >= timedelta(days=recheck_days)
