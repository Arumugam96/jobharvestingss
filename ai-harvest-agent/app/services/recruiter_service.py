"""Recruiter identity resolution — one canonical RecruiterORM row per
person, merged across sources/runs by LinkedIn URL first, then normalized
name+company. Used by HarvestRunService.bulk_insert_scraped_jobs (the single
choke point every source's jobs pass through) so LinkedIn/Naukri/Dice all
get identity resolution for free instead of each downstream agent
(recruiter_contact_agent.py, lead_merge_agent.py, orchestrator_agent.py)
re-implementing its own dedup dict from JSON.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.harvest_run import ScrapedJobORM
from app.models.recruiter import RecruiterDiscoveryRunORM, RecruiterORM

_LEGAL_SUFFIXES = re.compile(r"\b(inc|incorporated|ltd|limited|llc|llp|pvt|private|corp|corporation|co)\b")
_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")


def normalize_linkedin_url(url: str) -> str:
    return url.split("?")[0].rstrip("/").lower().strip()


def _slug(text: str) -> str:
    text = (text or "").lower()
    text = _LEGAL_SUFFIXES.sub("", text)
    text = _PUNCTUATION.sub("", text)
    return _WHITESPACE.sub(" ", text).strip()


def compute_name_company_key(person_name: str, company_name: str) -> str:
    return f"{_slug(person_name)}|{_slug(company_name)}"


def compute_dedup_key(person_name: str, company_name: str, linkedin_profile_url: str | None) -> str:
    if linkedin_profile_url:
        return f"li:{normalize_linkedin_url(linkedin_profile_url)}"
    return f"nc:{compute_name_company_key(person_name, company_name)}"


async def upsert_recruiter(
    db: AsyncSession,
    *,
    person_name: str,
    company_name: str = "",
    designation: str = "",
    linkedin_profile_url: str | None = None,
    harvest_source: str = "",
) -> RecruiterORM | None:
    """Resolve-or-create the one RecruiterORM row for this person.

    Lookup order: LinkedIn URL (if given), then the persistent name+company
    key (RecruiterORM.name_company_key — always populated, unlike dedup_key
    which switches to the URL form once one is known). Using dedup_key
    itself for the fallback lookup would miss a recruiter who was first seen
    *with* a URL and is now posting a job *without* one, since their
    dedup_key would already be the "li:..." form, not "nc:...". Matching on
    name_company_key instead means the merge works in both directions: a
    recruiter first seen without a URL later gets promoted to the URL-based
    dedup_key (below), and one first seen with a URL still gets found by a
    later URL-less posting.
    """
    person_name = (person_name or "").strip()
    if not person_name:
        return None
    company_name = (company_name or "").strip()
    raw_url = (linkedin_profile_url or "").strip()
    li_url = normalize_linkedin_url(raw_url) if raw_url else None
    name_key = compute_name_company_key(person_name, company_name)

    existing: RecruiterORM | None = None
    if li_url:
        result = await db.execute(select(RecruiterORM).where(RecruiterORM.linkedin_profile_url == li_url))
        existing = result.scalar_one_or_none()
    if existing is None:
        result = await db.execute(select(RecruiterORM).where(RecruiterORM.name_company_key == name_key))
        existing = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)

    if existing is not None:
        if li_url and not existing.linkedin_profile_url:
            existing.linkedin_profile_url = li_url
            existing.dedup_key = compute_dedup_key(person_name, company_name, li_url)
        if company_name and not existing.company_name:
            existing.company_name = company_name
        if designation and not existing.designation:
            existing.designation = designation
        existing.last_seen_at = now
        await db.flush()
        return existing

    recruiter = RecruiterORM(
        id=str(uuid.uuid4()),
        dedup_key=compute_dedup_key(person_name, company_name, li_url),
        name_company_key=name_key,
        linkedin_profile_url=li_url,
        person_name=person_name,
        company_name=company_name,
        designation=designation,
        harvest_source=harvest_source,
        first_seen_at=now,
        last_seen_at=now,
    )
    try:
        # Scoped to a SAVEPOINT: on a concurrent-insert race (another
        # session upserting the same dedup_key at the same time) only this
        # insert rolls back — not the caller's whole bulk_insert_scraped_jobs
        # transaction, which may already hold earlier flushed job rows.
        async with db.begin_nested():
            db.add(recruiter)
            await db.flush()
    except IntegrityError:
        result = await db.execute(select(RecruiterORM).where(RecruiterORM.dedup_key == recruiter.dedup_key))
        return result.scalar_one_or_none()
    return recruiter


async def save_enrichment(
    db: AsyncSession,
    recruiter_id: str,
    *,
    company_domain: str = "",
    company_website: str = "",
    official_email_id: str = "",
    email_status: str = "NOT_FOUND",
    contact_number: str = "",
    phone_status: str = "NOT_FOUND",
    linkedin_headline: str = "",
    location: str = "",
    reporting_hierarchy: str = "",
    position_level: str = "NOT_FOUND",
    employment_type: str = "NOT_FOUND",
    years_in_company: str = "NOT_FOUND",
    overall_experience: str = "NOT_FOUND",
    hiring_domain: str = "NOT_FOUND",
    company_industry: str = "NOT_FOUND",
    company_size: str = "NOT_FOUND",
    department: str = "",
    confidence_score: str = "Low",
    verified: bool = False,
) -> None:
    """Cache one recruiter's scraped contact-discovery result on their
    RecruiterORM row, so RecruiterContactAgent and ProspectIntelligenceAgent
    share the same enrichment instead of each re-scraping the same person.

    Only overwrites a field when the new value is non-default/non-empty —
    a re-enrichment run that finds *less* than a previous one (e.g. a
    LinkedIn profile that's since gone private) shouldn't erase what was
    already verified. last_enriched_at is stamped on every call (we always
    attempted enrichment); last_verified only when `verified=True` — i.e.
    contact info was actually confirmed present this pass, not merely attempted.
    """
    recruiter = await db.get(RecruiterORM, recruiter_id)
    if recruiter is None:
        return

    def _apply(field: str, value: str, not_found: str = "") -> None:
        if value and value != not_found:
            setattr(recruiter, field, value)

    _apply("company_domain", company_domain)
    _apply("company_website", company_website)
    _apply("official_email_id", official_email_id)
    if email_status != "NOT_FOUND":
        recruiter.email_status = email_status
    _apply("contact_number", contact_number)
    if phone_status != "NOT_FOUND":
        recruiter.phone_status = phone_status
    _apply("linkedin_headline", linkedin_headline)
    _apply("location", location)
    _apply("reporting_hierarchy", reporting_hierarchy)
    _apply("position_level", position_level, "NOT_FOUND")
    _apply("employment_type", employment_type, "NOT_FOUND")
    _apply("years_in_company", years_in_company, "NOT_FOUND")
    _apply("overall_experience", overall_experience, "NOT_FOUND")
    _apply("hiring_domain", hiring_domain, "NOT_FOUND")
    _apply("company_industry", company_industry, "NOT_FOUND")
    _apply("company_size", company_size, "NOT_FOUND")
    _apply("department", department)
    if confidence_score and confidence_score != "Low":
        recruiter.confidence_score = confidence_score
    now = datetime.now(timezone.utc)
    recruiter.last_enriched_at = now
    if verified:
        recruiter.last_verified = now
    await db.flush()


async def link_recruiter_jobs_by_url(db: AsyncSession, recruiter_id: str, linkedin_profile_url: str) -> int:
    """Backfill: point any ScrapedJobORM row matching this exact recruiter
    URL — but not yet linked — at the canonical RecruiterORM row.

    HarvestRunService.bulk_insert_scraped_jobs already sets recruiter_id at
    insert time for new rows via upsert_recruiter; this only catches
    pre-existing orphaned rows (e.g. from a harvest run that predates this
    recruiter's identity being resolved). Narrowed with a LIKE on the URL's
    host+path before the exact normalized comparison, so this doesn't have
    to full-table-scan every orphaned job for every recruiter in a run.
    Returns the number of rows updated.
    """
    norm_target = normalize_linkedin_url(linkedin_profile_url)
    host_path = norm_target.split("://", 1)[-1]
    if not host_path:
        return 0

    result = await db.execute(
        select(ScrapedJobORM).where(
            ScrapedJobORM.linkedin_profile_url.ilike(f"%{host_path}%"),
            ScrapedJobORM.recruiter_id.is_(None),
        )
    )
    updated = 0
    for job in result.scalars():
        if normalize_linkedin_url(job.linkedin_profile_url or "") == norm_target:
            job.recruiter_id = recruiter_id
            updated += 1
    if updated:
        await db.flush()
    return updated


# ── Recruiter discovery run history (small — mirrors HarvestRunORM's shape,
# not ScrapedJobORM's; see RecruiterDiscoveryRunORM's docstring) ────────────────

async def save_discovery_run(
    db: AsyncSession,
    *,
    run_id: str,
    source_filter: str,
    harvest_sources: list[str],
    total_recruiters: int,
    enriched: int,
    high_confidence: int,
    medium_confidence: int,
    low_confidence: int,
    verified_emails: int,
    public_emails: int,
    verified_phones: int,
    public_phones: int,
    no_contact: int,
    runtime_minutes: float,
    json_path: str = "",
    excel_path: str = "",
    debug_path: str = "",
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> None:
    db.add(RecruiterDiscoveryRunORM(
        id=str(uuid.uuid4()),
        run_id=run_id,
        source_filter=source_filter,
        harvest_sources=harvest_sources,
        total_recruiters=total_recruiters,
        enriched=enriched,
        high_confidence=high_confidence,
        medium_confidence=medium_confidence,
        low_confidence=low_confidence,
        verified_emails=verified_emails,
        public_emails=public_emails,
        verified_phones=verified_phones,
        public_phones=public_phones,
        no_contact=no_contact,
        runtime_minutes=runtime_minutes,
        json_path=json_path or None,
        excel_path=excel_path or None,
        debug_path=debug_path or None,
        started_at=started_at,
        completed_at=completed_at,
    ))
    await db.flush()


async def list_discovery_runs(db: AsyncSession, limit: int = 50) -> list[RecruiterDiscoveryRunORM]:
    result = await db.execute(
        select(RecruiterDiscoveryRunORM).order_by(RecruiterDiscoveryRunORM.created_at.desc()).limit(limit)
    )
    return list(result.scalars())


async def get_discovery_run(db: AsyncSession, run_id: str) -> RecruiterDiscoveryRunORM | None:
    result = await db.execute(
        select(RecruiterDiscoveryRunORM).where(RecruiterDiscoveryRunORM.run_id == run_id)
    )
    return result.scalar_one_or_none()
