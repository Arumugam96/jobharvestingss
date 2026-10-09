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
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import structlog
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.persona_titles import titles_for_persona, titles_for_personas
from app.core.tenant_context import apply_tenant, get_current_tenant_id, set_current_tenant
from app.models.recruiter_finder import (
    RecruiterFinderItemORM,
    RecruiterFinderJobORM,
    RecruiterFinderRevealLogORM,
)
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
        # Richer Apollo fields kept for the History reveal log (not shown in the
        # deduped recruiter record). Tolerant of ApolloOrgResult or a SimpleNamespace.
        "person_id": ((getattr(match, "id", None) or getattr(person, "id", None) or "") if match else ""),
        "secondary_email": (getattr(match, "secondary_email", "") or "") if match else "",
        "linkedin_url": ((getattr(match, "linkedin_url", "") or getattr(person, "linkedin_url", "") or "") if match else ""),
        "industry": (getattr(org, "industry", "") or "") if org else "",
        "company_size": (getattr(org, "size", None) if org else None),
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
    client: ApolloClient, item: dict, title_groups: list[list[str]],
    want_email: bool, want_phone: bool, reveal_cap: int,
) -> dict:
    """Find a contact AT a company (company-only row), honoring the ordered "Who to look
    up" roles. ``title_groups`` is one Apollo-title list per selected role, in the user's
    selection order. Resolve the org once, then walk the groups top-to-bottom: search each
    role's titles and reveal candidates until ONE yields a contact, then stop — so we
    return the highest-priority role that had a hit. Total reveal attempts across ALL roles
    are bounded by ``reveal_cap`` so a company with many empty roles can't balloon Apollo
    usage."""
    company, domain, location = item["company"], item["domain"], item["location"]
    org = None
    if domain:
        org = await client.enrich_organization(domain)
    if org is None or not org.id:
        org = await client.search_organization(company, domain=domain)
    if org is None or not org.id:
        return _blank_outcome("not_found")

    domain_out = org.domain or domain or ""
    person_locations = [location] if location else None
    attempts = 0
    saw_candidate = False
    seen: set[str] = set()
    for titles in title_groups:
        if attempts >= reveal_cap:
            break
        if not titles:
            continue
        people = await client.search_people([org.id], titles, person_locations=person_locations)
        if not people:
            continue
        saw_candidate = True
        for person in people:
            if attempts >= reveal_cap:
                break
            pid = getattr(person, "id", None)
            if not pid or pid in seen:
                continue
            seen.add(pid)
            attempts += 1
            try:
                match = await client.match_person_by_id(pid, reveal_email=want_email, reveal_phone=want_phone)
            except ApolloAPIError:
                continue
            if (want_email and match.email) or (want_phone and match.phone):
                return _completed_outcome(match, person, org)
    # Candidates existed but none yielded a revealable contact → ambiguous (needs review);
    # no candidate at all for any role → not_found.
    return _blank_outcome("ambiguous" if saw_candidate else "not_found", company_domain=domain_out)


async def _company_lookup_many(
    client: ApolloClient, item: dict, titles: list[str], want_email: bool, want_phone: bool,
    want_count: int, reveal_cap: int, reserve,
) -> tuple[list[dict], str, bool]:
    """Single-search multi-reveal: find up to ``want_count`` DISTINCT persona contacts
    AT a company. Walks the candidate list, revealing one at a time, and reserves a
    credit via ``reserve()`` ONLY for each contact it actually delivers (so a miss costs
    nothing). Stops at ``want_count`` successes, when candidates run out, or when the
    budget is exhausted mid-run. Returns (results, fallback_status, budget_exhausted);
    ``fallback_status`` is only meaningful when ``results`` is empty."""
    company, domain, location = item["company"], item["domain"], item["location"]
    org = None
    if domain:
        org = await client.enrich_organization(domain)
    if org is None or not org.id:
        org = await client.search_organization(company, domain=domain)
    if org is None or not org.id:
        return [], "not_found", False
    domain_out = org.domain or domain or ""

    person_locations = [location] if location else None
    # Fetch more candidates than wanted so failed reveals have slack (reveal_cap).
    fetch_n = min(100, max(10, want_count + reveal_cap))
    people = await client.search_people(
        [org.id], titles, person_locations=person_locations, per_page=fetch_n
    )
    if not people:
        return [], "not_found", False

    results: list[dict] = []
    seen: set[str] = set()
    budget_exhausted = False
    for person in people:
        if len(results) >= want_count:
            break
        pid = getattr(person, "id", None)
        if not pid or pid in seen:
            continue
        seen.add(pid)
        try:
            match = await client.match_person_by_id(pid, reveal_email=want_email, reveal_phone=want_phone)
        except ApolloAPIError:
            continue
        if (want_email and match.email) or (want_phone and match.phone):
            if not await reserve():  # out of per-tenant budget — keep what we have
                budget_exhausted = True
                break
            out = _completed_outcome(match, person, org)
            out["company_domain"] = out["company_domain"] or domain_out
            results.append(out)
    # Candidates existed but none yielded a revealable contact → ambiguous (needs review).
    return results, ("ambiguous" if not results else "completed"), budget_exhausted


async def _enrich_item(
    client: ApolloClient, item: dict, title_groups: list[list[str]],
    want_email: bool, want_phone: bool, reveal_cap: int,
) -> dict:
    """A row with a person (name/LinkedIn) enriches that person; a company-only row walks
    the ordered "Who to look up" roles (``title_groups``) and reveals the first that yields
    a contact. Any Apollo/other error → ``failed``."""
    try:
        if item["linkedin_url"] or item["person_name"]:
            return await _person_lookup(client, item, want_email, want_phone)
        return await _company_lookup(client, item, title_groups, want_email, want_phone, reveal_cap)
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
        "personas": [p for p in (job.personas or "").split("\n") if p],
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
    source: str, tenant_id: str, requested_by: str | None, personas: list[str] | None = None,
) -> str:
    """Persist a job + one item per VALID row (pending), owned by ``tenant_id``.
    Uses the request session so the route controls commit/rollback. Returns job id.

    ``personas`` is the ordered multi-select "Who to look up" (roles and/or free-text
    titles); it is stored newline-joined and drives the per-company priority walk. The
    legacy single ``persona`` is kept for display and as the fallback when the list is empty."""
    valid = [r for r in rows if r.get("validation_status") == "valid"]
    personas_clean = [p.strip() for p in (personas or []) if p and p.strip()]
    job = RecruiterFinderJobORM(
        id=str(uuid.uuid4()), tenant_id=tenant_id, requested_by=requested_by,
        source=source, filename=filename or "",
        persona=(persona or (personas_clean[0] if personas_clean else "")),
        personas="\n".join(personas_clean),
        reveal=reveal or "email",
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
    # Ordered "Who to look up" roles → one Apollo-title list per role, in selection order,
    # so a company-only row is searched highest-priority role first. Resolve each selection
    # on its own (known persona → its titles; free-text → used verbatim). Fall back to the
    # single legacy persona when the list is empty.
    personas = jobd.get("personas") or []
    if personas:
        title_groups = [titles_for_personas([p]) for p in personas]
    else:
        title_groups = [titles_for_persona(jobd["persona"])]
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

            outcome = await _enrich_item(client, item, title_groups, want_email, want_phone, reveal_cap)

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


async def _persist_completed(
    outcome: dict, item: dict, requested_by: str | None, tenant: str,
    *, reveal_type: str = "email", source: str = "", cost: int = 0,
) -> str | None:
    """Upsert one completed lookup into `recruiters` (source_label='contact_finder') AND
    append a row to recruiter_finder_reveal_log (the History audit trail) in the SAME
    transaction. Returns the recruiter id."""
    async def _work(db, o=outcome, it=item):
        recruiter_id = await save_finder_contact(
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
        )
        db.add(RecruiterFinderRevealLogORM(
            id=str(uuid.uuid4()), tenant_id=tenant,
            person_id=o.get("person_id", "") or "",
            contact_name=o["contact_name"] or it["person_name"] or "",
            contact_title=o["contact_title"],
            company=it["company"],
            company_domain=o["company_domain"] or it["domain"] or "",
            email=o["email"], email_status=o["email_status"] or "",
            secondary_email=o.get("secondary_email", "") or "",
            phone=o["phone"], phone_status="PUBLIC" if o["phone"] else "NOT_FOUND",
            linkedin_url=it["linkedin_url"] or o.get("linkedin_url", "") or "",
            city=o["city"], state=o["state"], country=o["country"],
            industry=o.get("industry", "") or "", company_size=o.get("company_size"),
            confidence=o["confidence"] or "Low", reveal_type=reveal_type,
            credits_spent=cost, source=source, requested_by=requested_by,
            recruiter_id=recruiter_id,
        ))
        await db.flush()
        return recruiter_id
    return await db_write(_work)


async def run_single(
    *, company: str = "", location: str = "", persona: str = "", reveal: str = "email",
    person_name: str = "", linkedin_url: str = "", domain: str = "", count: int = 1,
    requested_by: str | None = None,
) -> dict:
    """Synchronous single lookup. A known person (name+company or LinkedIn) enriches
    exactly ONE contact; a company lookup reveals up to ``count`` DISTINCT persona
    contacts. A per-tenant credit is reserved for each contact ACTUALLY revealed (a
    miss costs nothing), bounded by the workspace's remaining daily budget. Every hit
    is upserted into `recruiters`. Runs in the request context (tenant already bound).

    Returns ``{status, requested, revealed, credits_spent, budget_exhausted,
    company_domain, message, results: [outcome+recruiter_id, …]}``."""
    settings = get_settings()
    tenant = get_current_tenant_id()
    want_email, want_phone, cost = _reveal_flags(reveal)

    def _resp(status, *, results=None, requested=1, budget_exhausted=False, message="", domain_out=""):
        results = results or []
        return {
            "status": status, "requested": requested, "revealed": len(results),
            "credits_spent": len(results) * cost, "budget_exhausted": budget_exhausted,
            "company_domain": domain_out, "message": message, "results": results,
        }

    if not settings.apollo_api_key:
        return _resp("unconfigured", message="Apollo is not configured (set APOLLO_API_KEY).")

    is_person = bool((person_name or "").strip() or (linkedin_url or "").strip())
    want_count = 1 if is_person else max(1, min(int(count or 1), settings.company_contact_max_count))

    cap = await resolve_tenant_cap(tenant)
    # Pre-flight budget check (no spend): short-circuit when the workspace can't afford
    # even one contact, and never try to reveal more than today's budget allows.
    remaining = await recruiter_finder_budget.remaining_today(tenant, cap=cap)  # None = unlimited
    if remaining is not None and remaining < cost:
        return _resp("over_budget", requested=want_count,
                     message="Daily Apollo budget reached for this workspace — try again tomorrow.")
    if remaining is not None:
        want_count = min(want_count, remaining // cost)

    async def reserve():
        return await recruiter_finder_budget.try_consume(cost, tenant, cap=cap)

    client = ApolloClient(settings)
    item = {
        "id": None, "company": company or "", "person_name": person_name or "",
        "linkedin_url": linkedin_url or "", "domain": domain or "", "location": location or "", "title": "",
    }
    titles = titles_for_persona(persona)

    if is_person:
        try:
            outcome = await _person_lookup(client, item, want_email, want_phone)
        except ApolloAPIError as exc:
            outcome = _blank_outcome("failed", error=str(exc))
        except Exception as exc:  # defensive — mirror _enrich_item
            logger.warning("recruiter_finder_single_error", error=str(exc))
            outcome = _blank_outcome("failed", error=str(exc))
        if outcome["status"] != "completed":
            return _resp(outcome["status"], requested=1, message=outcome.get("error", ""),
                         domain_out=outcome.get("company_domain", ""))
        if not await reserve():
            return _resp("over_budget", requested=1,
                         message="Daily Apollo budget reached for this workspace — try again tomorrow.")
        outcome["recruiter_id"] = await _persist_completed(
            outcome, item, requested_by, tenant, reveal_type=reveal, source="person", cost=cost)
        return _resp("completed", results=[outcome], requested=1,
                     domain_out=outcome.get("company_domain", ""))

    results, fallback, budget_exhausted = await _company_lookup_many(
        client, item, titles, want_email, want_phone, want_count,
        settings.company_contact_reveal_cap, reserve,
    )
    for o in results:
        o["recruiter_id"] = await _persist_completed(
            o, item, requested_by, tenant, reveal_type=reveal, source="company_single", cost=cost)
    domain_out = results[0].get("company_domain", "") if results else ""
    status = "completed" if results else fallback
    return _resp(status, results=results, requested=want_count,
                 budget_exhausted=budget_exhausted, domain_out=domain_out)


# ── Browse → select → reveal (company mode; Apollo-UI style) ────────────────────

def _candidate_dict(p) -> dict:
    """One row for the browse table from a (locked) search_people result. No email is
    revealed here — email stays locked; email_status hints whether one can be unlocked."""
    org = getattr(p, "organization", None)
    loc = ", ".join(x for x in (getattr(p, "city", None), getattr(p, "state", None),
                                getattr(p, "country", None)) if x)
    return {
        "person_id": getattr(p, "id", None) or "",
        "name": getattr(p, "name", "") or "",
        "title": getattr(p, "title", "") or "",
        "linkedin_url": getattr(p, "linkedin_url", "") or "",
        "location": loc,
        "city": getattr(p, "city", "") or "",
        "state": getattr(p, "state", "") or "",
        "country": getattr(p, "country", "") or "",
        "email_status": getattr(p, "email_status", "") or "",
        "company_domain": (org.domain if org else "") or "",
    }


async def list_company_candidates(
    *, company: str = "", location: str = "", persona: str = "",
    personas: list[str] | None = None, domain: str = "", max_candidates: int = 500,
) -> dict:
    """List a company's persona candidates (LOCKED emails) for the browse table.
    Paginates search_people over up to 5 free pages (per_page=100) — reveals nothing,
    so it spends NO credit (neither the per-tenant Finder budget nor, past page 1, the
    account-wide cap). ``personas`` (the multi-select "Who to look up" input — known
    persona labels and/or free-text titles) takes precedence over the single ``persona``.
    Returns ``{status, company_domain, total, candidates: [...]}``."""
    settings = get_settings()
    if not settings.apollo_api_key:
        return {"status": "unconfigured", "company_domain": "", "total": 0, "candidates": [],
                "message": "Apollo is not configured (set APOLLO_API_KEY)."}

    client = ApolloClient(settings)
    org = None
    if domain:
        org = await client.enrich_organization(domain)
    if org is None or not org.id:
        org = await client.search_organization(company, domain=domain)
    if org is None or not org.id:
        return {"status": "not_found", "company_domain": domain or "", "total": 0, "candidates": []}

    domain_out = org.domain or domain or ""
    titles = titles_for_personas(personas) if personas else titles_for_persona(persona)
    person_locations = [location] if location else None
    per_page = 100
    max_pages = max(1, min(5, -(-max_candidates // per_page)))  # ceil, capped at 5

    candidates: list[dict] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        people = await client.search_people(
            [org.id], titles, person_locations=person_locations,
            per_page=per_page, page=page, reserve=(page == 1),
        )
        if not people:
            break
        for p in people:
            pid = getattr(p, "id", None)
            if not pid or pid in seen:
                continue
            seen.add(pid)
            candidates.append(_candidate_dict(p))
            if len(candidates) >= max_candidates:
                break
        if len(candidates) >= max_candidates or len(people) < per_page:
            break

    status = "ok" if candidates else "not_found"
    return {"status": status, "company_domain": domain_out,
            "total": len(candidates), "candidates": candidates}


async def reveal_candidates(
    *, selections: list[dict], reveal: str = "email", requested_by: str | None = None,
) -> dict:
    """Reveal the emails/phones of hand-picked candidates (by Apollo person id) and
    upsert each hit into `recruiters`. A per-tenant credit is reserved for each contact
    ACTUALLY revealed (a miss costs nothing), bounded by the workspace's remaining daily
    budget. Returns ``{status, requested, revealed, credits_spent, budget_exhausted,
    results: [{person_id, status, email, email_status, phone, confidence, recruiter_id}]}``."""
    settings = get_settings()
    tenant = get_current_tenant_id()
    want_email, want_phone, cost = _reveal_flags(reveal)
    requested = len([s for s in selections if s.get("person_id")])

    def _resp(status, *, results=None, budget_exhausted=False, message=""):
        results = results or []
        revealed = sum(1 for r in results if r["status"] == "completed")
        return {"status": status, "requested": requested, "revealed": revealed,
                "credits_spent": revealed * cost, "budget_exhausted": budget_exhausted,
                "message": message, "results": results}

    if not settings.apollo_api_key:
        return _resp("unconfigured", message="Apollo is not configured (set APOLLO_API_KEY).")
    if requested == 0:
        return _resp("empty", message="No contacts selected.")

    cap = await resolve_tenant_cap(tenant)
    remaining = await recruiter_finder_budget.remaining_today(tenant, cap=cap)  # None = unlimited
    if remaining is not None and remaining < cost:
        return _resp("over_budget",
                     message="Daily Apollo budget reached for this workspace — try again tomorrow.")

    client = ApolloClient(settings)
    results: list[dict] = []
    budget_exhausted = False
    for sel in selections:
        pid = sel.get("person_id")
        if not pid:
            continue
        try:
            match = await client.match_person_by_id(pid, reveal_email=want_email, reveal_phone=want_phone)
        except ApolloAPIError as exc:
            results.append({"person_id": pid, "status": "failed", "email": "", "email_status": "",
                            "phone": "", "confidence": "", "recruiter_id": None, "error": str(exc)})
            continue
        if not ((want_email and match.email) or (want_phone and match.phone)):
            results.append({"person_id": pid, "status": "not_found", "email": "", "email_status": "",
                            "phone": "", "confidence": "", "recruiter_id": None, "error": ""})
            continue
        if not await recruiter_finder_budget.try_consume(cost, tenant, cap=cap):
            budget_exhausted = True
            break
        # Build the outcome from the reveal + the selection's company/domain context.
        item = {
            "id": None, "company": sel.get("company", "") or "", "person_name": sel.get("name", "") or "",
            "linkedin_url": sel.get("linkedin_url", "") or "", "domain": sel.get("domain", "") or "",
            "location": sel.get("location", "") or "", "title": sel.get("title", "") or "",
        }
        person = SimpleNamespace(name=sel.get("name", ""), title=sel.get("title", ""),
                                 id=pid, linkedin_url=sel.get("linkedin_url", ""))
        # Prefer the match's own organization (carries industry/size) for the History log.
        org = getattr(match, "organization", None) or SimpleNamespace(domain=sel.get("domain", "") or "")
        outcome = _completed_outcome(match, person, org)
        outcome["company_domain"] = outcome["company_domain"] or sel.get("domain", "") or ""
        recruiter_id = await _persist_completed(
            outcome, item, requested_by, tenant, reveal_type=reveal, source="company_browse", cost=cost)
        results.append({
            "person_id": pid, "status": "completed", "email": outcome["email"],
            "email_status": outcome["email_status"], "phone": outcome["phone"],
            "confidence": outcome["confidence"], "contact_name": outcome["contact_name"],
            "contact_title": outcome["contact_title"], "company_domain": outcome["company_domain"],
            "recruiter_id": recruiter_id, "error": "",
        })

    revealed = sum(1 for r in results if r["status"] == "completed")
    status = "completed" if revealed else ("over_budget" if budget_exhausted else "not_found")
    return _resp(status, results=results, budget_exhausted=budget_exhausted)


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


def _reveal_log_dict(r) -> dict:
    loc = ", ".join(x for x in (r.city, r.state, r.country) if x)
    return {
        "id": r.id, "person_id": r.person_id,
        "contact_name": r.contact_name, "contact_title": r.contact_title,
        "company": r.company, "company_domain": r.company_domain,
        "email": r.email, "email_status": r.email_status, "secondary_email": r.secondary_email,
        "phone": r.phone, "phone_status": r.phone_status, "linkedin_url": r.linkedin_url,
        "location": loc, "city": r.city, "state": r.state, "country": r.country,
        "industry": r.industry, "company_size": r.company_size,
        "confidence": r.confidence, "reveal_type": r.reveal_type,
        "credits_spent": r.credits_spent, "source": r.source,
        "requested_by": r.requested_by, "recruiter_id": r.recruiter_id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


async def history_log(
    db: AsyncSession, *, q: str = "", company: str = "", email_status: str = "",
    confidence: str = "", period: str = "", page: int = 1, page_size: int = 25,
) -> dict:
    """Filtered, paginated reveal history for the History tab, plus tenant-wide KPIs and
    filter facets. All queries tenant-scoped via apply_tenant. KPIs/facets are computed
    over the whole workspace history (not the current filter); only ``rows``/``total``
    reflect the filters."""
    L = RecruiterFinderRevealLogORM

    def scoped(stmt):
        return apply_tenant(stmt, L.tenant_id)

    # KPIs (tenant-wide, unfiltered)
    total_all = (await db.execute(scoped(select(func.count()).select_from(L)))).scalar() or 0
    verified = (await db.execute(scoped(
        select(func.count()).select_from(L).where(func.lower(L.email_status).in_(["verified", "valid"]))
    ))).scalar() or 0
    credits = (await db.execute(scoped(select(func.coalesce(func.sum(L.credits_spent), 0)).select_from(L)))).scalar() or 0
    this_week = (await db.execute(scoped(
        select(func.count()).select_from(L).where(L.created_at >= _now() - timedelta(days=7))
    ))).scalar() or 0

    # Facets for the filter dropdowns
    companies = (await db.execute(scoped(
        select(L.company).where(L.company != "").group_by(L.company).order_by(L.company).limit(300)
    ))).scalars().all()
    statuses = (await db.execute(scoped(
        select(L.email_status).where(L.email_status != "").group_by(L.email_status)
    ))).scalars().all()

    # Filtered page
    base = scoped(select(L))
    if q:
        like = f"%{q.lower()}%"
        base = base.where(or_(
            func.lower(L.contact_name).like(like), func.lower(L.contact_title).like(like),
            func.lower(L.company).like(like), func.lower(L.email).like(like),
        ))
    if company:
        base = base.where(L.company == company)
    if email_status:
        base = base.where(L.email_status == email_status)
    if confidence:
        base = base.where(L.confidence == confidence)
    if period == "today":
        base = base.where(L.created_at >= _now().replace(hour=0, minute=0, second=0, microsecond=0))
    elif period == "week":
        base = base.where(L.created_at >= _now() - timedelta(days=7))
    elif period == "month":
        base = base.where(L.created_at >= _now() - timedelta(days=30))

    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    page = max(1, page)
    page_size = max(1, min(200, page_size))
    rows = (await db.execute(
        base.order_by(L.created_at.desc()).limit(page_size).offset((page - 1) * page_size)
    )).scalars().all()

    return {
        "total": int(total), "page": page, "page_size": page_size,
        "kpis": {
            "revealed_total": int(total_all),
            "verified_pct": round(verified / total_all * 100) if total_all else 0,
            "this_week": int(this_week), "credits_spent": int(credits),
        },
        "facets": {"companies": list(companies), "email_statuses": list(statuses)},
        "rows": [_reveal_log_dict(r) for r in rows],
    }


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
