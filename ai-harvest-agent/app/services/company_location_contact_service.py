"""Company + job-location HR-contact discovery + cache.

When a harvested job has NO resolvable recruiter/poster email, we still want to reach
someone at that company — but a company can post from multiple locations with different
recruiters, so the contact is discovered and cached by **(company, job-location)**,
never by company alone, and the Apollo people search is ALWAYS location-constrained with
no company-wide fallback.

This module owns:
  * the CompanyLocationContactORM cache (find/upsert + the recheck-cooldown predicate),
  * the HR shortlist behind it (CompanyLocationCandidateORM) and the outreach rotation
    over that shortlist, and
  * ``discover_company_location_contacts`` — a best-effort, browser-free post-harvest pass
    (called from LinkedInAgent._run / NaukriAgent._run after the company pass) that
    finds the email-less jobs, groups them by (company_key, location_key), and for each
    group (re)builds the shortlist when needed and picks today's contact.

Rotation (one person per company+location per day, so nobody is flooded):
  * the shortlist is the top company_contact_shortlist_size HR people from Apollo's FREE
    people search, refreshed every apollo_recheck_days; resolving the company to an
    Apollo org costs a credit but is cached per company (resolve_apollo_org), not per
    location;
  * the pick is the person AFTER the one emailed most recently, in rank order, wrapping
    back to the first after the last; someone already emailed today stays the pick (the
    send-time guard then holds the send);
  * a person is skipped when their email is suppressed or they ever replied; an email
    is revealed (1 credit) only when the rotation reaches that person, and a reveal
    that finds no (verified) email marks them ``no_email`` so it's never paid twice.

The pass writes ONLY the cache: the pick is copied onto the parent row's hr_contact_*
fields, attached to each email-less job at insert time by
HarvestRunService.bulk_insert_scraped_jobs (looked up by the same keys), exactly like
CompanyORM feeds company size/HQ — so the original poster fields are never touched.
``mark_contact_emailed`` (auto-outreach, after a send) advances the rotation.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.company_domain import infer_company_domain, is_placeholder_company
from app.core.dependencies import get_session_factory
from app.core.location import location_search_terms, normalize_location_key
from app.models.harvest_run import CompanyLocationCandidateORM, CompanyLocationContactORM
from app.services import apollo_budget, run_guard
from app.services.apollo_client import ApolloAPIError, ApolloClient
from app.services.apollo_enrichment import apollo_hr_shortlist
from app.services.company_service import get_companies_by_keys, normalize_company_key
from app.services.outreach_log_service import recipient_has_replied
from app.services.reply_to_rotation import day_start
from app.services.suppression_service import is_suppressed
from app.services.recruiter_service import (
    compute_name_company_key,
    official_emails_by_name_company_keys,
)

logger = structlog.get_logger(__name__)

# Max distinct (company, location) pairs to Apollo-search per run — bounds search
# volume/time the same way company_enrichment_service.COMPANY_ENRICH_CAP bounds the company size pass. The daily
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
) -> CompanyLocationContactORM:
    """Insert-or-update one (company, location) contact cache row. Non-empty-wins merge
    (never erases a previous hit with a later miss); stamps hr_contact_attempted_at on
    every real attempt so the recheck cooldown can gate re-searches of negatives too.
    Returns the row."""
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
    return row


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


# ── HR shortlist + rotation ─────────────────────────────────────────────────────

def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands timestamps back naive (stored as UTC) — make them comparable."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def shortlist_is_stale(row: CompanyLocationContactORM | None, recheck_days: int) -> bool:
    """The (free) Apollo shortlist search is due: never searched, or last searched
    ``recheck_days`` or more ago (people leave / join)."""
    last = _aware(row.hr_contact_attempted_at) if row is not None else None
    return last is None or (datetime.now(timezone.utc) - last) >= timedelta(days=recheck_days)


async def get_candidates(
    db: AsyncSession, company_key: str, location_key: str
) -> list[CompanyLocationCandidateORM]:
    """Every shortlist row for one (company, location), active or not, best rank first."""
    result = await db.execute(
        select(CompanyLocationCandidateORM)
        .where(
            CompanyLocationCandidateORM.company_key == company_key,
            CompanyLocationCandidateORM.location_key == location_key,
        )
        .order_by(CompanyLocationCandidateORM.rank)
    )
    return list(result.scalars())


async def save_shortlist(
    db: AsyncSession, company_key: str, location_key: str, people: list
) -> list[CompanyLocationCandidateORM]:
    """Store a fresh ranked shortlist (ApolloPersonResult list, best first). People
    seen before keep their revealed email / no_email flag / last_emailed_at (so the
    rotation and paid reveals survive a refresh); people no longer returned are
    deactivated, not deleted. Returns the active rows, best rank first."""
    existing = {c.apollo_person_id: c for c in await get_candidates(db, company_key, location_key)}
    kept: set[str] = set()
    for rank, person in enumerate(people):
        pid = (getattr(person, "id", None) or "").strip()
        if not pid or pid in kept:
            continue
        kept.add(pid)
        row = existing.get(pid)
        if row is None:
            row = CompanyLocationCandidateORM(
                id=str(uuid.uuid4()), company_key=company_key, location_key=location_key,
                apollo_person_id=pid,
            )
            db.add(row)
            existing[pid] = row
        row.name = (getattr(person, "name", None) or row.name or "")[:300]
        row.title = (getattr(person, "title", None) or row.title or "")[:300]
        row.rank = rank
        row.active = True
    for pid, row in existing.items():
        if pid not in kept:
            row.active = False
    await db.flush()
    return sorted((r for r in existing.values() if r.active), key=lambda r: r.rank)


def rotation_order(
    candidates: list[CompanyLocationCandidateORM], today_start: datetime
) -> tuple[CompanyLocationCandidateORM | None, list[CompanyLocationCandidateORM]]:
    """(already_today, order). When someone was emailed since ``today_start`` they
    stay today's contact (already_today) — one person per company+location per day.
    Otherwise ``order`` is the active shortlist starting right AFTER the person
    emailed most recently, wrapping around (first after the last); from the top
    when nobody was emailed yet."""
    active = sorted((c for c in candidates if c.active), key=lambda c: c.rank)
    emailed = [c for c in active if c.last_emailed_at is not None]
    last = max(emailed, key=lambda c: _aware(c.last_emailed_at), default=None)
    if last is not None and _aware(last.last_emailed_at) >= today_start:
        return last, []
    start = active.index(last) + 1 if last is not None else 0
    return None, active[start:] + active[:start]


async def _contactable(db: AsyncSession, email: str) -> bool:
    return not await is_suppressed(db, email) and not await recipient_has_replied(db, email)


async def pick_rotation_contact(
    db: AsyncSession,
    candidates: list[CompanyLocationCandidateORM],
    *,
    client: ApolloClient | None,
    verified_only: bool,
    reveal_cap: int,
    today_start: datetime,
) -> tuple[CompanyLocationCandidateORM | None, int, bool]:
    """Today's contact for one (company, location): (pick, reveals_spent, cap_hit).

    Walks rotation_order. A person with a stored email is used unless suppressed or
    they ever replied. A person without one is revealed now (1 credit) — at most
    ``reveal_cap`` reveals, never past the daily Apollo budget; a reveal that finds
    no email (or, with ``verified_only``, a non-verified one) marks them no_email.
    When a reveal isn't possible right now the walk moves on to someone already
    revealed rather than stopping. None when nobody is contactable today."""
    already, order = rotation_order(candidates, today_start)
    if already is not None:
        return already, 0, False
    revealed = 0
    cap_hit = False
    for cand in order:
        if cand.no_email:
            continue
        if not cand.email:
            if client is None or cap_hit or revealed >= reveal_cap:
                continue
            remaining = await apollo_budget.remaining_today()
            if remaining is not None and remaining <= 0:
                cap_hit = True          # a cap denial is not "no email" — no flag
                continue
            revealed += 1
            try:
                match = await client.match_person_by_id(cand.apollo_person_id, reveal_email=True)
            except ApolloAPIError as exc:
                logger.debug("company_contact_reveal_failed", person_id=cand.apollo_person_id,
                             error=str(exc))
                continue
            status = (match.email_status or "").strip().lower()
            cand.revealed_at = datetime.now(timezone.utc)
            cand.email_status = status
            if not match.email or (verified_only and status and status != "verified"):
                cand.no_email = True
                continue
            cand.email = match.email.strip()
            if match.name and not cand.name:
                cand.name = match.name[:300]
        if await _contactable(db, cand.email):
            return cand, revealed, cap_hit
    return None, revealed, cap_hit


async def set_location_pick(
    db: AsyncSession,
    *,
    company_key: str,
    location_key: str,
    company_name: str,
    location: str,
    domain: str,
    pick: CompanyLocationCandidateORM | None,
    searched: bool,
) -> None:
    """Copy today's pick onto the parent cache row — what bulk_insert_scraped_jobs
    attaches. No pick clears it, so nobody is emailed for this pair today."""
    row = await upsert_location_contact(
        db, company_key=company_key, location_key=location_key,
        company_name=company_name, location=location, domain=domain,
        hr_contact_name="", hr_contact_title="", hr_contact_email="", hr_contact_phone="",
        attempted=searched,
    )
    row.hr_contact_name = pick.name if pick else ""
    row.hr_contact_title = pick.title if pick else ""
    row.hr_contact_email = pick.email if pick else ""
    row.hr_contact_phone = ""
    await db.flush()


async def mark_contact_emailed(
    db: AsyncSession, company_name: str, location: str, email: str,
    when: datetime | None = None,
) -> int:
    """Advance the rotation: stamp last_emailed_at on the shortlist person with this
    email at this (company, location). Returns rows stamped (0 for a contact that
    isn't on a shortlist, e.g. a pre-shortlist cache row)."""
    ckey, lkey = normalize_company_key(company_name or ""), normalize_location_key(location or "")
    addr = (email or "").strip().lower()
    if not (ckey and lkey and addr):
        return 0
    rows = [c for c in await get_candidates(db, ckey, lkey) if c.email.lower() == addr]
    stamp = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    for c in rows:
        c.last_emailed_at = stamp
    await db.flush()
    return len(rows)


async def location_emailed_since(
    db: AsyncSession, company_name: str, location: str, since: datetime
) -> bool:
    """True when anyone on this (company, location)'s shortlist was emailed at or
    after ``since`` — the one-email-per-company+location-per-day guard."""
    ckey, lkey = normalize_company_key(company_name or ""), normalize_location_key(location or "")
    if not (ckey and lkey):
        return False
    since_utc = since.astimezone(timezone.utc)
    return any(
        c.last_emailed_at is not None and _aware(c.last_emailed_at) >= since_utc
        for c in await get_candidates(db, ckey, lkey)
    )


# ── Discovery pass ──────────────────────────────────────────────────────────────

def _job_attr(job, name: str) -> str:
    return (getattr(job, name, "") or "").strip()


async def discover_company_location_contacts(
    jobs: list, settings: Settings, client: ApolloClient | None = None,
) -> dict:
    """Best-effort post-harvest pass: for every email-less harvested job, make sure
    its (company, job-location) has an HR shortlist and pick today's contact from it
    (see the module docstring for the rotation).

    Writes ONLY the cache — the pick is attached to jobs at insert time by
    bulk_insert_scraped_jobs. Never raises; returns a counts summary.
    """
    counts = {
        "searched": 0, "found": 0, "reused": 0, "revealed": 0, "no_pick": 0,
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
            if not company_name or is_placeholder_company(company_name):
                continue  # "Unknown Company" etc. is not a company to search
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

        client = client or ApolloClient(settings)
        today_start = day_start()
        shortlist_size = int(getattr(settings, "company_contact_shortlist_size", 10) or 10)
        verified_only = bool(getattr(settings, "company_contact_verified_only", True))
        reveal_cap = max(1, int(settings.company_contact_reveal_cap))

        # 4. Per pair, largest groups first: refresh the shortlist when due (free),
        #    then pick today's contact (reveals only the person picked).
        ordered = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)
        for (ckey, lkey), group_jobs in ordered:
            if run_guard.is_stop_requested():
                logger.info("company_location_contact_stopped_by_user", searched=counts["searched"])
                break

            row = cache.get((ckey, lkey))
            sample = group_jobs[0]
            company_name = _job_attr(sample, "company")
            location_raw = _job_attr(sample, "location")
            comp_row = company_rows.get(ckey)
            domain = (comp_row.domain if comp_row else "") or infer_company_domain(company_name)[0]

            try:
                async with session_factory() as db:
                    candidates = await get_candidates(db, ckey, lkey)
                    # A pre-shortlist cache row (an email, no candidates) is migrated now.
                    legacy = row is not None and bool(row.hr_contact_email) and not candidates
                    due = shortlist_is_stale(row, settings.apollo_recheck_days) or legacy
                    if due and counts["searched"] >= _COMPANY_CONTACT_CAP:
                        logger.info("company_location_contact_cap_reached", cap=_COMPANY_CONTACT_CAP)
                        due = False
                    searched = False
                    cap_deferred = False
                    if due:
                        shortlist = await apollo_hr_shortlist(
                            settings=settings, company_name=company_name,
                            location_terms=location_search_terms(location_raw),
                            # only a KNOWN domain — the name-inferred guess is the
                            # resolver's own fallback, never trusted over its cache
                            domain=(comp_row.domain if comp_row else ""), size=shortlist_size,
                            verified_only=verified_only, client=client,
                        )
                        if shortlist.cap_reached:
                            cap_deferred = True           # nothing stamped — retried next run
                            counts["cap_deferred"] += 1
                        if shortlist.attempted:
                            searched = True
                            counts["searched"] += 1
                            domain = shortlist.company_domain or domain
                            candidates = await save_shortlist(db, ckey, lkey, shortlist.people)
                    if not candidates and not searched:
                        if not cap_deferred:
                            counts["skipped_cooldown"] += 1   # recent empty search — wait it out
                        continue

                    pick, revealed, cap_hit = await pick_rotation_contact(
                        db, candidates, client=client, verified_only=verified_only,
                        reveal_cap=reveal_cap, today_start=today_start,
                    )
                    counts["revealed"] += revealed
                    counts["cap_deferred"] += int(cap_hit)
                    if pick is None:
                        counts["no_pick"] += 1
                    elif revealed:
                        counts["found"] += 1
                    else:
                        counts["reused"] += 1
                    await set_location_pick(
                        db, company_key=ckey, location_key=lkey,
                        company_name=company_name, location=location_raw, domain=domain,
                        pick=pick, searched=searched,
                    )
                    await db.commit()
                    logger.info(
                        "company_location_contact_picked", company=company_name,
                        location=location_raw, shortlist=len([c for c in candidates if c.active]),
                        rank=(pick.rank if pick else None), revealed=revealed,
                    )
            except Exception as exc:
                logger.warning("company_location_contact_pair_failed", company=company_name, error=str(exc))

        logger.info("company_location_contact_pass_complete", **counts)
        return {"enabled": True, **counts}
    except Exception as exc:
        logger.warning("company_location_contact_discovery_failed", error=str(exc))
        return {"error": str(exc), **counts}
