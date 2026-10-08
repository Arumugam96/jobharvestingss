"""Recruiter Contact Finder — the Apollo enrichment engine behind the /contacts page.

Mirrors the harvest's async contract (app/routes/run_harvest_agent.py): a bulk upload
persists a job + per-row items, returns immediately, and a DETACHED asyncio task drains
the items through Apollo while the UI polls GET /recruiter-finder/jobs/{id}. Single
lookups run synchronously.

Reuse, not rewrite:
  * ApolloClient / apollo_enrichment for the actual Apollo calls;
  * recruiter_finder_budget for the per-tenant daily cap (reserve a credit BEFORE each
    contact; when a workspace's budget is spent, remaining rows go ``queued``);
  * recruiter_service.save_finder_contact to upsert the hit into `recruiters` (tagged
    source_label="contact_finder" + requested_by) so finder contacts dedupe with
    harvested ones and flow into outreach;
  * harvest_run_service.db_read/db_write for best-effort DB access from the detached
    task (its own short-lived session — no FastAPI DI available there).

NOTE: deliberately does NOT use run_guard — that single-flight guard exists only to
serialize the shared Chrome profile; Apollo is HTTP-only and runs alongside harvests.
The global apollo_daily_cap still applies at the ApolloClient chokepoint as the
account-wide backstop.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.persona_titles import titles_for_persona
from app.core.tenant_context import apply_tenant, get_current_tenant_id, set_current_tenant
from app.models.recruiter_finder import RecruiterFinderItemORM, RecruiterFinderJobORM
from app.services import recruiter_finder_budget
from app.services.apollo_client import ApolloAPIError, ApolloClient
from app.services.harvest_run_service import db_read, db_write
from app.services.recruiter_service import save_finder_contact

logger = structlog.get_logger(__name__)

_STATUS_COUNTERS = ("completed", "not_found", "ambiguous", "failed")


def _now():
    return datetime.now(timezone.utc)


def _reveal_flags(reveal: str) -> tuple[bool, bool, int]:
    """(want_email, want_phone, cost) for a reveal choice. Email=1, Phone=1, Both=2
    credits per contact (min 1). Phone is still subject to Apollo's webhook requirement
    inside ApolloClient — without APOLLO_WEBHOOK_URL it is auto-downgraded to email."""
    r = (reveal or "email").lower()
    want_email = r in ("email", "both")
    want_phone = r in ("phone", "both")
    if not want_email and not want_phone:
        want_email = True
    return want_email, want_phone, max(1, int(want_email) + int(want_phone))


def _confidence(email_status: str, has_email: bool, has_phone: bool) -> str:
    if (email_status or "").lower() in ("verified", "valid"):
        return "High"
    if has_email or has_phone:
        return "Medium"
    return "Low"


def _recruiter_email_status(outcome: dict) -> str:
    if not outcome.get("email"):
        return "NOT_FOUND"
    return "VERIFIED" if (outcome.get("email_status") or "").lower() in ("verified", "valid") else "PUBLIC"


def _blank_outcome(status: str, **extra) -> dict:
    out = {
        "status": status, "contact_name": "", "contact_title": "", "email": "",
        "email_status": "", "phone": "", "confidence": "", "company_domain": "",
        "city": "", "state": "", "country": "", "error": "",
    }
    out.update(extra)
    return out


def _completed_outcome(match, person, org) -> dict:
    email = (match.email or "") if match else ""
    phone = (match.phone or "") if match else ""
    email_status = (match.email_status or "") if match else ""
    return {
        "status": "completed",
        "contact_name": (match.name or getattr(person, "name", "") or "") if match else "",
        "contact_title": (match.title or getattr(person, "title", "") or "") if match else "",
        "email": email,
        "email_status": email_status,
        "phone": phone,
        "confidence": _confidence(email_status, bool(email), bool(phone)),
        "company_domain": (org.domain if org else "") or "",
        "city": (match.city or "") if match else "",
        "state": (match.state or "") if match else "",
        "country": (match.country or "") if match else "",
        "error": "",
    }


# ── Apollo lookups ────────────────────────────────────────────────────────────

async def _person_lookup(client: ApolloClient, item: dict, want_email: bool, want_phone: bool) -> dict:
    """Enrich a KNOWN person (by LinkedIn URL, else name + company)."""
    if item["linkedin_url"]:
        person = await client.enrich_person_by_linkedin(
            item["linkedin_url"], reveal_email=want_email, reveal_phone=want_phone,
            name=item["person_name"], company=item["company"], domain=item["domain"],
        )
    else:
        results = await client.bulk_enrich_people(
            [{"name": item["person_name"], "organization_name": item["company"], "domain": item["domain"]}],
            reveal_email=want_email, reveal_phone=want_phone,
        )
        person = results[0] if results else None
    if person is None or not person.matched:
        return _blank_outcome("not_found")
    if (want_email and person.email) or (want_phone and person.phone):
        return _completed_outcome(person, person, person.organization)
    return _blank_outcome("not_found", company_domain=(person.organization.domain if person.organization else ""))


async def _company_lookup(
    client: ApolloClient, item: dict, titles: list[str], want_email: bool, want_phone: bool, reveal_cap: int
) -> dict:
    """Find the chosen persona AT a company (company-only row)."""
    company, domain, location = item["company"], item["domain"], item["location"]
    org = None
    if domain:
        org = await client.enrich_organization(domain)
    if org is None or not org.id:
        org = await client.search_organization(company, domain=domain)
    if org is None or not org.id:
        return _blank_outcome("not_found")

    person_locations = [location] if location else None
    people = await client.search_people([org.id], titles, person_locations=person_locations)
    if not people:
        return _blank_outcome("not_found", company_domain=(org.domain or domain or ""))

    revealed = 0
    for person in people:
        if revealed >= reveal_cap:
            break
        pid = getattr(person, "id", None)
        if not pid:
            continue
        revealed += 1
        try:
            match = await client.match_person_by_id(pid, reveal_email=want_email, reveal_phone=want_phone)
        except ApolloAPIError:
            continue
        if (want_email and match.email) or (want_phone and match.phone):
            return _completed_outcome(match, person, org)
    # Candidates existed but none yielded a revealable contact → ambiguous (needs review).
    return _blank_outcome("ambiguous", company_domain=(org.domain or domain or ""))


async def _enrich_item(
    client: ApolloClient, item: dict, titles: list[str], want_email: bool, want_phone: bool, reveal_cap: int
) -> dict:
    """A row with a person (name/LinkedIn) enriches that person; a company-only row
    finds the persona at the company. Any Apollo/other error → ``failed``."""
    try:
        if item["linkedin_url"] or item["person_name"]:
            return await _person_lookup(client, item, want_email, want_phone)
        return await _company_lookup(client, item, titles, want_email, want_phone, reveal_cap)
    except ApolloAPIError as exc:
        return _blank_outcome("failed", error=str(exc))
    except Exception as exc:  # defensive — one bad row must not kill the job
        logger.warning("recruiter_finder_item_error", item_id=item.get("id"), error=str(exc))
        return _blank_outcome("failed", error=str(exc))


# ── Job + item persistence (small, mirror HarvestRunService.update_run) ──────────

async def _load_job_dict(db: AsyncSession, job_id: str) -> dict | None:
    job = await db.get(RecruiterFinderJobORM, job_id)
    if job is None:
        return None
    return {
        "id": job.id, "tenant_id": job.tenant_id, "persona": job.persona,
        "reveal": job.reveal, "requested_by": job.requested_by, "total": job.total,
    }


async def _mark_job_running(db: AsyncSession, job_id: str) -> None:
    job = await db.get(RecruiterFinderJobORM, job_id)
    if job is not None:
        job.status = "running"
        job.started_at = _now()
        job.message = "Starting enrichment…"
        await db.flush()


async def _set_job_message(db: AsyncSession, job_id: str, message: str) -> None:
    job = await db.get(RecruiterFinderJobORM, job_id)
    if job is not None:
        job.message = message
        await db.flush()


async def _next_pending_item(db: AsyncSession, job_id: str) -> dict | None:
    stmt = (
        select(RecruiterFinderItemORM)
        .where(RecruiterFinderItemORM.job_id == job_id, RecruiterFinderItemORM.status == "pending")
        .order_by(RecruiterFinderItemORM.row_index)
        .limit(1)
    )
    item = (await db.execute(stmt)).scalar_one_or_none()
    if item is None:
        return None
    return {k: getattr(item, k) for k in
            ("id", "company", "person_name", "linkedin_url", "domain", "location", "title")}


async def _mark_item_processing(db: AsyncSession, item_id: str) -> None:
    item = await db.get(RecruiterFinderItemORM, item_id)
    if item is not None:
        item.status = "processing"
        item.last_attempt_at = _now()
        await db.flush()


async def _apply_item_result(db: AsyncSession, item_id: str, outcome: dict, recruiter_id: str | None) -> None:
    item = await db.get(RecruiterFinderItemORM, item_id)
    if item is None:
        return
    item.status = outcome["status"]
    item.contact_name = outcome["contact_name"]
    item.contact_title = outcome["contact_title"]
    item.email = outcome["email"]
    item.email_status = outcome["email_status"]
    item.phone = outcome["phone"]
    item.confidence = outcome["confidence"]
    item.error = outcome["error"]
    if recruiter_id:
        item.recruiter_id = recruiter_id
    item.attempts += 1
    item.last_attempt_at = _now()
    await db.flush()


async def _bump_counters(db: AsyncSession, job_id: str, status: str, cost: int) -> None:
    job = await db.get(RecruiterFinderJobORM, job_id)
    if job is None:
        return
    job.processed += 1
    if status in _STATUS_COUNTERS:
        setattr(job, status, getattr(job, status) + 1)
    job.credits_spent += cost
    denom = max(1, job.total - job.queued)
    job.progress = min(100, round(job.processed / denom * 100))
    await db.flush()


async def _queue_remaining(db: AsyncSession, job_id: str) -> int:
    result = await db.execute(
        update(RecruiterFinderItemORM)
        .where(RecruiterFinderItemORM.job_id == job_id, RecruiterFinderItemORM.status == "pending")
        .values(status="queued", updated_at=_now())
    )
    n = result.rowcount or 0
    job = await db.get(RecruiterFinderJobORM, job_id)
    if job is not None:
        job.queued += n
        await db.flush()
    return n


async def _finalize_job(db: AsyncSession, job_id: str) -> None:
    job = await db.get(RecruiterFinderJobORM, job_id)
    if job is None:
        return
    if job.queued > 0:
        job.status = "partial"
    elif job.failed > 0 and job.completed == 0 and job.not_found == 0 and job.ambiguous == 0:
        job.status = "failed"
    else:
        job.status = "completed"
    job.completed_at = _now()
    denom = max(1, job.total - job.queued)
    job.progress = min(100, round(job.processed / denom * 100))
    job.message = ""
    await db.flush()


async def _fail_job_unconfigured(db: AsyncSession, job_id: str) -> None:
    await db.execute(
        update(RecruiterFinderItemORM)
        .where(RecruiterFinderItemORM.job_id == job_id, RecruiterFinderItemORM.status == "pending")
        .values(status="failed", error="Apollo is not configured", updated_at=_now())
    )
    job = await db.get(RecruiterFinderJobORM, job_id)
    if job is not None:
        job.status = "failed"
        job.failed = job.total
        job.processed = job.total
        job.progress = 100
        job.message = "Apollo is not configured (set APOLLO_API_KEY)."
        job.completed_at = _now()
        await db.flush()


async def _recount_job(db: AsyncSession, job: RecruiterFinderJobORM) -> None:
    rows = await db.execute(
        select(RecruiterFinderItemORM.status, func.count())
        .where(RecruiterFinderItemORM.job_id == job.id)
        .group_by(RecruiterFinderItemORM.status)
    )
    counts = {s: c for s, c in rows.all()}
    job.completed = counts.get("completed", 0)
    job.not_found = counts.get("not_found", 0)
    job.ambiguous = counts.get("ambiguous", 0)
    job.failed = counts.get("failed", 0)
    job.queued = counts.get("queued", 0)
    job.processed = job.completed + job.not_found + job.ambiguous + job.failed
    denom = max(1, job.total - job.queued)
    job.progress = min(100, round(job.processed / denom * 100))


# ── Public API ────────────────────────────────────────────────────────────────

async def resolve_tenant_cap(tenant_id: str) -> int:
    """Effective per-tenant daily cap: the tenant's config override, else the global
    setting. Defensive — any failure falls back to the setting."""
    settings = get_settings()
    default = settings.recruiter_finder_daily_cap
    try:
        from app.models.tenant import TenantORM
        tenant = await db_read(lambda db: db.get(TenantORM, tenant_id))
        config = getattr(tenant, "config", None) if tenant is not None else None
        if isinstance(config, dict):
            val = config.get("recruiter_finder_daily_cap")
            if isinstance(val, int):
                return val
    except Exception as exc:
        logger.debug("recruiter_finder_cap_resolve_failed", tenant_id=tenant_id, error=str(exc))
    return default


async def create_job_with_items(
    db: AsyncSession, *, rows: list[dict], persona: str, reveal: str, filename: str,
    source: str, tenant_id: str, requested_by: str | None,
) -> str:
    """Persist a job + one item per VALID row (pending), owned by ``tenant_id``.
    Uses the request session so the route controls commit/rollback. Returns job id."""
    valid = [r for r in rows if r.get("validation_status") == "valid"]
    job = RecruiterFinderJobORM(
        id=str(uuid.uuid4()), tenant_id=tenant_id, requested_by=requested_by,
        source=source, filename=filename or "", persona=persona or "", reveal=reveal or "email",
        status="queued", total=len(valid), message="Queued",
    )
    db.add(job)
    await db.flush()
    for r in valid:
        db.add(RecruiterFinderItemORM(
            id=str(uuid.uuid4()), tenant_id=tenant_id, job_id=job.id,
            row_index=int(r.get("row_index", 0) or 0),
            company=r.get("company", ""), person_name=r.get("person_name", ""),
            linkedin_url=r.get("linkedin_url", ""), domain=r.get("domain", ""),
            location=r.get("location", ""), title=r.get("title", ""),
            validation_status="valid", status="pending",
        ))
    await db.flush()
    return job.id


def launch_job(job_id: str) -> None:
    """Fire-and-forget the detached enrichment worker (mirrors /run-harvest-agent)."""
    asyncio.create_task(_run_enrichment_background(job_id), name=f"enrich-{job_id}")


async def _run_enrichment_background(job_id: str) -> None:
    settings = get_settings()
    jobd = await db_read(lambda db: _load_job_dict(db, job_id))
    if not jobd:
        logger.warning("recruiter_finder_job_missing", job_id=job_id)
        return
    tenant = jobd["tenant_id"]
    # The detached task has no request tenant bound — set it so db_write/db_read scope
    # their own sessions to this job's tenant.
    set_current_tenant(tenant)
    await db_write(lambda db: _mark_job_running(db, job_id))

    if not settings.apollo_api_key:
        await db_write(lambda db: _fail_job_unconfigured(db, job_id))
        return

    want_email, want_phone, cost = _reveal_flags(jobd["reveal"])
    titles = titles_for_persona(jobd["persona"])
    reveal_cap = settings.company_contact_reveal_cap
    cap = await resolve_tenant_cap(tenant)
    requested_by = jobd["requested_by"]
    client = ApolloClient(settings)

    try:
        while True:
            item = await db_read(lambda db: _next_pending_item(db, job_id))
            if not item:
                break
            if not await recruiter_finder_budget.try_consume(cost, tenant, cap=cap):
                await db_write(lambda db: _queue_remaining(db, job_id))
                logger.info("recruiter_finder_budget_exhausted", job_id=job_id, tenant_id=tenant)
                break
            await db_write(lambda db, iid=item["id"]: _mark_item_processing(db, iid))
            label = item["person_name"] or item["company"] or "contact"
            await db_write(lambda db, m=f"Enriching {label}…": _set_job_message(db, job_id, m))

            outcome = await _enrich_item(client, item, titles, want_email, want_phone, reveal_cap)

            recruiter_id = None
            if outcome["status"] == "completed":
                recruiter_id = await db_write(lambda db, o=outcome, it=item: save_finder_contact(
                    db,
                    person_name=o["contact_name"] or it["person_name"] or "",
                    company_name=it["company"],
                    designation=o["contact_title"],
                    linkedin_profile_url=it["linkedin_url"] or None,
                    company_domain=o["company_domain"] or it["domain"],
                    official_email_id=o["email"],
                    email_status=_recruiter_email_status(o),
                    contact_number=o["phone"],
                    phone_status="PUBLIC" if o["phone"] else "NOT_FOUND",
                    city=o["city"], state=o["state"], country=o["country"],
                    confidence_score=o["confidence"] or "Low",
                    verified=bool(o["email"] or o["phone"]),
                    requested_by=requested_by, tenant_id=tenant,
                ))
            await db_write(lambda db, iid=item["id"], o=outcome, rid=recruiter_id:
                           _apply_item_result(db, iid, o, rid))
            await db_write(lambda db, st=outcome["status"]: _bump_counters(db, job_id, st, cost))
    finally:
        await db_write(lambda db: _finalize_job(db, job_id))
    logger.info("recruiter_finder_job_done", job_id=job_id)


async def run_single(
    *, company: str = "", location: str = "", persona: str = "", reveal: str = "email",
    person_name: str = "", linkedin_url: str = "", domain: str = "", requested_by: str | None = None,
) -> dict:
    """Synchronous single lookup (company→recruiter or known person). Reserves a
    per-tenant credit, enriches, upserts the hit into recruiters, and returns the
    contact. Runs in the request context (tenant already bound)."""
    settings = get_settings()
    tenant = get_current_tenant_id()
    want_email, want_phone, cost = _reveal_flags(reveal)
    if not settings.apollo_api_key:
        return {"status": "unconfigured", "message": "Apollo is not configured (set APOLLO_API_KEY)."}
    cap = await resolve_tenant_cap(tenant)
    if not await recruiter_finder_budget.try_consume(cost, tenant, cap=cap):
        return {"status": "over_budget",
                "message": "Daily Apollo budget reached for this workspace — try again tomorrow."}

    client = ApolloClient(settings)
    item = {
        "id": None, "company": company or "", "person_name": person_name or "",
        "linkedin_url": linkedin_url or "", "domain": domain or "", "location": location or "", "title": "",
    }
    titles = titles_for_persona(persona)
    outcome = await _enrich_item(client, item, titles, want_email, want_phone, settings.company_contact_reveal_cap)

    recruiter_id = None
    if outcome["status"] == "completed":
        recruiter_id = await db_write(lambda db, o=outcome, it=item: save_finder_contact(
            db,
            person_name=o["contact_name"] or it["person_name"] or "",
            company_name=it["company"],
            designation=o["contact_title"],
            linkedin_profile_url=it["linkedin_url"] or None,
            company_domain=o["company_domain"] or it["domain"],
            official_email_id=o["email"],
            email_status=_recruiter_email_status(o),
            contact_number=o["phone"],
            phone_status="PUBLIC" if o["phone"] else "NOT_FOUND",
            city=o["city"], state=o["state"], country=o["country"],
            confidence_score=o["confidence"] or "Low",
            verified=bool(o["email"] or o["phone"]),
            requested_by=requested_by, tenant_id=tenant,
        ))
    return {**outcome, "recruiter_id": recruiter_id}


# ── Reads for the routes (tenant-scoped via apply_tenant) ───────────────────────

async def get_job(db: AsyncSession, job_id: str) -> RecruiterFinderJobORM | None:
    stmt = apply_tenant(
        select(RecruiterFinderJobORM).where(RecruiterFinderJobORM.id == job_id),
        RecruiterFinderJobORM.tenant_id,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_items(
    db: AsyncSession, job_id: str, *, statuses: list[str] | None = None, limit: int = 25, offset: int = 0
) -> tuple[list[RecruiterFinderItemORM], int]:
    base = select(RecruiterFinderItemORM).where(RecruiterFinderItemORM.job_id == job_id)
    base = apply_tenant(base, RecruiterFinderItemORM.tenant_id)
    if statuses:
        base = base.where(RecruiterFinderItemORM.status.in_(statuses))
    count_stmt = select(func.count()).select_from(base.subquery())
    total = (await db.execute(count_stmt)).scalar() or 0
    rows = (await db.execute(
        base.order_by(RecruiterFinderItemORM.row_index).limit(limit).offset(offset)
    )).scalars().all()
    return list(rows), int(total)


async def list_jobs(db: AsyncSession, *, limit: int = 50) -> list[RecruiterFinderJobORM]:
    stmt = apply_tenant(
        select(RecruiterFinderJobORM).order_by(RecruiterFinderJobORM.created_at.desc()).limit(limit),
        RecruiterFinderJobORM.tenant_id,
    )
    return list((await db.execute(stmt)).scalars().all())


async def list_recent_contacts(db: AsyncSession, *, limit: int = 12) -> list:
    """Recent contacts the Finder has written to `recruiters` (source_label='contact_finder'),
    tenant-scoped — proves persistence and gives the Single page some real sample data."""
    from app.models.recruiter import RecruiterORM
    stmt = apply_tenant(
        select(RecruiterORM)
        .where(RecruiterORM.source_label == "contact_finder")
        .order_by(RecruiterORM.updated_at.desc())
        .limit(limit),
        RecruiterORM.tenant_id,
    )
    return list((await db.execute(stmt)).scalars().all())


async def retry_job(db: AsyncSession, job_id: str) -> bool:
    """Reset this job's unsuccessful + budget-deferred rows (failed/not_found/queued)
    back to pending and re-queue the job — successes/ambiguous are left untouched, so
    a retry never re-spends on records that already resolved."""
    job = await get_job(db, job_id)
    if job is None:
        return False
    await db.execute(
        update(RecruiterFinderItemORM)
        .where(
            RecruiterFinderItemORM.job_id == job_id,
            RecruiterFinderItemORM.status.in_(["failed", "not_found", "queued"]),
        )
        .values(status="pending", error="", updated_at=_now())
    )
    await _recount_job(db, job)
    job.status = "queued"
    job.message = "Retrying unsuccessful records…"
    job.completed_at = None
    await db.flush()
    return True


async def fail_stale_jobs() -> int:
    """Startup reaper (called from app/main.py lifespan): a detached worker doesn't
    survive a restart, so mark orphaned running/queued jobs failed and their in-flight
    items failed so a retry can pick them up. Mirrors HarvestRunService.fail_stale_running."""
    async def _reap(db: AsyncSession) -> int:
        await db.execute(
            update(RecruiterFinderItemORM)
            .where(
                RecruiterFinderItemORM.status == "processing",
                RecruiterFinderItemORM.job_id.in_(
                    select(RecruiterFinderJobORM.id).where(
                        RecruiterFinderJobORM.status.in_(["running", "queued"])
                    )
                ),
            )
            .values(status="failed", error="Interrupted by a restart", updated_at=_now())
        )
        result = await db.execute(
            update(RecruiterFinderJobORM)
            .where(RecruiterFinderJobORM.status.in_(["running", "queued"]))
            .values(status="failed", message="Interrupted by a restart — retry to resume.",
                    completed_at=_now())
        )
        return result.rowcount or 0

    return await db_write(_reap) or 0
