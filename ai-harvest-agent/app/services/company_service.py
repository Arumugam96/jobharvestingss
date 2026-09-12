"""Company-level enrichment cache + Apollo organization-enrichment pass.

Why this exists
───────────────
The per-recruiter Apollo people-match (app/agents/linkedin_agent._enrich_recruiters)
returns a company's size / HQ location for free — but ONLY for jobs whose poster
Apollo matched. Jobs with no recruiter (or whose recruiter already had an email,
so Apollo was never called) got no company size/location.

This module closes that gap: for every unique company in a run it fetches company
data ONCE — via Apollo's organizations/enrich (keyed on a domain derived from the
company name / a domain already learned from a recruiter match) — and caches it on
CompanyORM. HarvestRunService.bulk_insert_scraped_jobs then fills EVERY job of that
company from the cache, recruiter or not.

Credit discipline: the whole pass is gated by settings.apollo_enrich_company
(default off), a per-company recheck cooldown (settings.apollo_recheck_days) avoids
re-billing, and misses are remembered (apollo_attempted) so they aren't retried
every run.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.company_domain import infer_company_domain
from app.core.company_size import band_from_employee_count
from app.models.harvest_run import CompanyORM

logger = structlog.get_logger(__name__)

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


async def _upsert_company(
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
        row.apollo_enriched_at = now
    await db.flush()


def _needs_enrichment(row: CompanyORM | None, recheck_days: int) -> bool:
    """A company needs an Apollo lookup when it has no size yet and either was
    never tried or its cooldown has elapsed."""
    if row is not None and row.company_size:
        return False  # already have a size — nothing to fetch
    if row is None or row.apollo_enriched_at is None:
        return True
    last = row.apollo_enriched_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last) >= timedelta(days=recheck_days)


async def enrich_companies(
    db: AsyncSession,
    settings: Settings,
    companies: list[dict],
    *,
    client=None,
    max_calls: int = 100,
) -> int:
    """Apollo organizations/enrich each unique company that still lacks a size.

    `companies` is a list of {"company_name": str, "domain": str (optional, a
    real domain already known e.g. from a recruiter's Apollo match)}. Returns the
    number of Apollo calls issued. No-op unless settings.apollo_enrich_company and
    an API key are set. Every company is individually try/excepted so one failure
    never aborts the pass.
    """
    if not settings.apollo_enrich_company or not settings.apollo_api_key:
        return 0

    from app.services.apollo_client import ApolloClient
    client = client or ApolloClient(settings)

    # Dedup by key, keeping the first non-empty known domain seen for it.
    wanted: dict[str, dict] = {}
    for c in companies:
        key = normalize_company_key(c.get("company_name", ""))
        if not key:
            continue
        slot = wanted.setdefault(key, {"company_name": c.get("company_name", ""), "domain": ""})
        if not slot["domain"] and c.get("domain"):
            slot["domain"] = c["domain"]

    cached = await get_companies_by_keys(db, set(wanted))
    calls = 0
    for key, info in wanted.items():
        if calls >= max_calls:
            logger.info("company_enrich_cap_reached", cap=max_calls)
            break
        if not _needs_enrichment(cached.get(key), settings.apollo_recheck_days):
            continue
        domain = (info["domain"] or infer_company_domain(info["company_name"])[0]).strip()
        if not domain:
            continue
        calls += 1
        size = country = state = industry = ""
        try:
            org = await client.enrich_organization(domain)
        except Exception as exc:
            org = None
            logger.debug("company_enrich_failed", domain=domain, error=str(exc))
        if org:
            size = band_from_employee_count(org.size)
            country = org.country or ""
            state = org.state or ""
            industry = org.industry or ""
        try:
            await _upsert_company(
                db,
                company_key=key,
                company_name=info["company_name"],
                domain=domain,
                company_size=size,
                company_country=country,
                company_state=state,
                company_industry=industry,
                apollo_attempted=True,
            )
            await db.commit()
        except Exception as exc:
            logger.warning("company_cache_write_failed", company=info["company_name"], error=str(exc))

    logger.info("company_enrichment_pass_complete", companies=len(wanted), apollo_calls=calls)
    return calls
