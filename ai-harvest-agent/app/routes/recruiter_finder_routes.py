"""Recruiter Contact Finder API (the /contacts page).

Endpoints (all under the shared auth guard; added to harvest-agent/nginx.conf):
  POST /recruiter-finder/validate              parse+map+validate an upload (no credits)
  POST /recruiter-finder/upload                create a bulk enrichment job → 202 {job_id}
  POST /recruiter-finder/search                single synchronous lookup
  GET  /recruiter-finder/jobs/{job_id}         job status + live counters (poll target)
  GET  /recruiter-finder/jobs/{job_id}/items   paginated per-row states (live table)
  POST /recruiter-finder/jobs/{job_id}/retry   re-queue unsuccessful/deferred rows
  GET  /recruiter-finder/usage                 per-tenant daily credit gauge + 14-day history
  GET  /recruiter-finder/history               recent jobs
  GET  /recruiter-finder/phone-status          poll async Apollo phone deliveries
  GET  /recruiter-finder/apollo-webhook-events recent raw webhook deliveries (diagnostics)
  POST /recruiter-finder/apollo-webhook-events/reprocess  backfill unmatched deliveries (internal)

Unauthenticated (``webhook_router`` — Apollo carries no login cookie; guarded by a
shared token in the URL query, like the outreach webhooks):
  POST /recruiter-finder/apollo-phone-webhook/{ref}  Apollo's async phone delivery
  POST /recruiter-finder/apollo-phone-webhook        legacy form (?ref=… in the query)

Thin trigger layer — all Apollo/budget/persistence logic lives in
app/services/recruiter_finder_service.py (+ _import / _budget).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import hmac
import json
import re

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.dependencies import get_current_user, get_db_session
from app.core.tenant_context import is_all_access
from app.services import apollo_budget, apollo_phone_service, recruiter_finder_budget
from app.services.recruiter_finder_import import parse_and_validate
from app.services.recruiter_finder_service import (
    _reveal_state,
    create_job_with_items,
    get_job,
    history_log,
    launch_job,
    list_company_candidates,
    list_items,
    list_jobs,
    list_recent_contacts,
    resolve_tenant_cap,
    retry_job,
    reveal_candidates,
    run_single,
)

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["Recruiter Contact Finder"])
# Mounted WITHOUT the auth guard in app/main.py (Apollo has no session cookie).
webhook_router = APIRouter(tags=["Recruiter Contact Finder"])


# ── Serializers ──────────────────────────────────────────────────────────────

def _job_dict(job, *, phones_pending: int | None = None) -> dict[str, Any]:
    """Serialise a finder job row for the API (counters, ISO timestamps, `personas` split per line).

    `phones_pending` is included only when passed.
    """
    out = {
        "job_id": job.id, "status": job.status, "progress": job.progress, "message": job.message,
        "source": job.source, "filename": job.filename, "persona": job.persona,
        "personas": [p for p in (job.personas or "").split("\n") if p], "reveal": job.reveal,
        "total": job.total, "processed": job.processed, "completed": job.completed,
        "not_found": job.not_found, "ambiguous": job.ambiguous, "failed": job.failed,
        "queued": job.queued, "credits_spent": job.credits_spent,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }
    if phones_pending is not None:
        out["phones_pending"] = phones_pending
    return out


def _item_dict(it) -> dict[str, Any]:
    """Serialise one finder item row, including the UI-facing `reveal_state`."""
    return {
        "id": it.id, "row_index": it.row_index, "company": it.company,
        "person_name": it.person_name, "linkedin_url": it.linkedin_url, "location": it.location,
        "validation_status": it.validation_status, "status": it.status,
        "contact_name": it.contact_name, "contact_title": it.contact_title,
        "email": it.email, "email_status": it.email_status, "phone": it.phone,
        "phone_status": it.phone_status or "", "person_id": it.apollo_person_id or "",
        # What the UI shows: "in_progress" while a completed row's phone is still PENDING.
        "reveal_state": _reveal_state(it.status, it.email, it.phone, it.phone_status),
        "confidence": it.confidence, "recruiter_id": it.recruiter_id,
        "error": it.error, "attempts": it.attempts,
    }


# ── Models ───────────────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    company: str = ""
    location: str = ""
    persona: str = Field(default="", description="B2B persona for a company lookup (default: Recruiters & HR)")
    reveal: str = Field(default="email", description="email | phone | both")
    person_name: str = ""
    linkedin_url: str = ""
    domain: str = ""
    count: int = Field(default=1, ge=1, le=100,
                       description="Company lookup only: how many distinct contacts to reveal "
                                   "(clamped server-side to company_contact_max_count; 1 for a person)")


class CandidatesRequest(BaseModel):
    company: str = ""
    location: str = ""
    persona: str = Field(default="", description="Single B2B persona (legacy; default: Recruiters & HR)")
    personas: list[str] = Field(default_factory=list,
                                description="Multi-select 'Who to look up' — persona labels and/or free-text titles")
    domain: str = ""


class Selection(BaseModel):
    person_id: str
    company: str = ""
    domain: str = ""
    name: str = ""
    title: str = ""
    linkedin_url: str = ""
    location: str = ""


class RevealRequest(BaseModel):
    reveal: str = Field(default="email", description="email | phone | both")
    selections: list[Selection] = Field(default_factory=list)


# ── Bulk: validate / upload ──────────────────────────────────────────────────

@router.post("/recruiter-finder/validate")
async def validate_upload(
    file: UploadFile = File(...),
    current_user=Depends(get_current_user),
) -> Any:
    """Parse + intelligently column-map + validate an uploaded CSV/XLSX and return a
    preview (mapping, per-row verdicts, summary). Spends NO Apollo credits."""
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    settings = get_settings()
    try:
        preview = parse_and_validate(
            content, file.filename or "upload.csv", max_rows=settings.recruiter_finder_max_rows
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return preview


@router.post("/recruiter-finder/upload", status_code=status.HTTP_202_ACCEPTED)
async def upload_and_enrich(
    file: UploadFile = File(...),
    persona: str = Form(""),
    personas: list[str] = Form(default=[]),
    reveal: str = Form("email"),
    db: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
) -> Any:
    """Re-validate the file server-side, persist a job + per-row items, launch the
    detached enrichment worker, and return ``202 {job_id}`` to poll."""
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    settings = get_settings()
    try:
        preview = parse_and_validate(
            content, file.filename or "upload.csv", max_rows=settings.recruiter_finder_max_rows
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if preview["summary"]["ready"] == 0:
        raise HTTPException(
            status_code=422,
            detail="No valid rows to enrich — every row is missing a company or is a duplicate.",
        )
    job_id = await create_job_with_items(
        db, rows=preview["rows"], persona=persona, personas=personas, reveal=reveal,
        filename=file.filename or "", source="upload",
        tenant_id=current_user.tenant_id, requested_by=current_user.email,
    )
    await db.commit()  # persist before the detached worker reads the job in its own session
    launch_job(job_id)
    logger.info("recruiter_finder_job_started", job_id=job_id, ready=preview["summary"]["ready"])
    return {"job_id": job_id, "summary": preview["summary"]}


# ── Single lookup ────────────────────────────────────────────────────────────

@router.post("/recruiter-finder/search")
async def single_search(
    body: SearchRequest,
    current_user=Depends(get_current_user),
) -> Any:
    """One synchronous lookup — a company (→ recruiter) or a known person (name+company
    or LinkedIn URL on its own)."""
    if not (body.company.strip() or body.person_name.strip() or body.linkedin_url.strip()):
        raise HTTPException(status_code=422, detail="Enter a company, a person's name, or a LinkedIn URL.")
    return await run_single(
        company=body.company, location=body.location, persona=body.persona, reveal=body.reveal,
        person_name=body.person_name, linkedin_url=body.linkedin_url, domain=body.domain,
        count=body.count, requested_by=current_user.email,
    )


@router.post("/recruiter-finder/candidates")
async def candidates(
    body: CandidatesRequest,
    current_user=Depends(get_current_user),
) -> Any:
    """List a company's persona candidates with LOCKED emails for the browse table.
    Reveals nothing, so it spends NO credit — the UI shows these free, then the user
    picks rows to reveal via POST /recruiter-finder/reveal."""
    if not body.company.strip():
        raise HTTPException(status_code=422, detail="Enter a company to browse its contacts.")
    return await list_company_candidates(
        company=body.company, location=body.location, persona=body.persona,
        personas=body.personas, domain=body.domain,
    )


@router.post("/recruiter-finder/reveal")
async def reveal(
    body: RevealRequest,
    current_user=Depends(get_current_user),
) -> Any:
    """Reveal the emails/phones of hand-picked candidates (by Apollo person id). Charges
    one credit per contact actually revealed (bounded by the daily cap) and upserts each
    hit into `recruiters`."""
    if not body.selections:
        raise HTTPException(status_code=422, detail="Select at least one contact to reveal.")
    return await reveal_candidates(
        selections=[s.model_dump() for s in body.selections],
        reveal=body.reveal, requested_by=current_user.email,
    )


# ── Job status / items / retry ───────────────────────────────────────────────

@router.get("/recruiter-finder/jobs/{job_id}")
async def job_status(
    job_id: str,
    db: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
) -> Any:
    """Return a bulk finder job's status and live counters (the UI's poll target).

    Includes `phones_pending`, the number of outstanding async Apollo phone deliveries; the UI keeps
    polling after completion while it is above zero. 404 if the job isn't in the caller's tenant.
    """
    job = await get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    # Outstanding Apollo phone deliveries — the UI keeps polling after completion
    # while this is > 0.
    return _job_dict(job, phones_pending=await apollo_phone_service.job_pending_count(db, job_id))


# UI filter buckets → the item statuses they include ("all" = no filter).
_BUCKETS: dict[str, list[str] | None] = {
    "all": None,
    "successful": ["completed"],
    "ambiguous": ["ambiguous"],
    "unsuccessful": ["not_found", "failed"],
    "queued": ["queued"],
}


@router.get("/recruiter-finder/jobs/{job_id}/items")
async def job_items(
    job_id: str,
    bucket: str = "all",
    page: int = 1,
    page_size: int = 25,
    db: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
) -> Any:
    """Page through a job's rows, optionally filtered by UI bucket.

    `bucket` is all | successful | ambiguous | unsuccessful | queued (an unknown value means no
    filter). `page_size` is clamped to 1-200. 404 if the job isn't in the caller's tenant.
    """
    job = await get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    page = max(1, page)
    page_size = max(1, min(200, page_size))
    statuses = _BUCKETS.get(bucket, None)
    rows, total = await list_items(
        db, job_id, statuses=statuses, limit=page_size, offset=(page - 1) * page_size
    )
    return {"total": total, "page": page, "page_size": page_size, "bucket": bucket,
            "items": [_item_dict(r) for r in rows]}


@router.post("/recruiter-finder/jobs/{job_id}/retry")
async def retry(
    job_id: str,
    db: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
) -> Any:
    """Re-queue a job's unsuccessful and budget-deferred rows and relaunch the background worker.

    Successful/ambiguous rows are left alone, so a retry never re-spends credits on them. Returns
    `{"job_id", "status": "queued"}`; 404 if the job isn't in the caller's tenant.
    """
    ok = await retry_job(db, job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found")
    await db.commit()
    launch_job(job_id)
    return {"job_id": job_id, "status": "queued"}


# ── Usage / history ──────────────────────────────────────────────────────────

@router.get("/recruiter-finder/usage")
async def usage(current_user=Depends(get_current_user)) -> Any:
    """Today's Recruiter Finder credit gauge for the caller's tenant plus 14 days of history.

    Also reports whether async phone reveal is enabled, its per-phone credit cost, pending phone
    deliveries, the pending-phone TTL and account-wide Apollo usage (global cap and sub-caps).
    """
    tenant = current_user.tenant_id
    cap = await resolve_tenant_cap(tenant)
    used = await recruiter_finder_budget.usage_today(tenant)
    remaining = await recruiter_finder_budget.remaining_today(tenant, cap=cap)
    history = await recruiter_finder_budget.usage_history(tenant, 14)
    return {
        "cap": cap, "used": used, "remaining": remaining, "history": history,
        # Async phone reveal (Apollo → webhook): offered only when a webhook URL is set;
        # each delivered phone costs phone_credit_cost credits, charged on delivery.
        "phone_enabled": apollo_phone_service.phone_enabled(),
        "phone_credit_cost": apollo_phone_service.phone_cost(),
        "phones_pending": await apollo_phone_service.pending_count(tenant),
        # A phone still pending after this long is treated as not found (the UI stops
        # polling a little after it).
        "phone_pending_ttl_minutes": get_settings().apollo_phone_pending_ttl_minutes,
        # Account-wide Apollo usage today (global ceiling + harvest enrichment sub-cap).
        "apollo": await apollo_budget.usage_today(),
    }


@router.get("/recruiter-finder/history")
async def history(
    db: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
) -> Any:
    """The 50 most recent bulk finder jobs of the caller's tenant, newest first."""
    jobs = await list_jobs(db, limit=50)
    return {"jobs": [_job_dict(j) for j in jobs]}


@router.get("/recruiter-finder/history-log")
async def history_log_endpoint(
    q: str = "",
    company: str = "",
    email_status: str = "",
    confidence: str = "",
    period: str = "",
    page: int = 1,
    page_size: int = 25,
    db: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
) -> Any:
    """Filterable, paginated history of every contact revealed from Single search
    (company browse or specific person) + tenant-wide KPIs and filter facets."""
    return await history_log(
        db, q=q.strip(), company=company, email_status=email_status, confidence=confidence,
        period=period, page=page, page_size=page_size,
    )


@router.get("/recruiter-finder/recent-contacts")
async def recent_contacts(
    db: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
) -> Any:
    """Recent Finder-sourced contacts from this workspace's `recruiters` store."""
    rows = await list_recent_contacts(db, limit=12)
    return {"contacts": [{
        "name": r.person_name, "title": r.designation, "company": r.company_name,
        "domain": r.company_domain, "email": r.official_email_id, "email_status": r.email_status,
        "phone": r.contact_number, "phone_status": r.phone_status, "confidence": r.confidence_score,
        "requested_by": r.requested_by, "linkedin_url": r.linkedin_profile_url,
    } for r in rows]}


# ── Async phone reveal (Apollo webhook) ──────────────────────────────────────

def _csv(value: str) -> list[str]:
    """Split a comma-separated query value into trimmed, non-empty parts."""
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@router.get("/recruiter-finder/phone-status")
async def phone_status(
    person_ids: str = Query(default="", description="Comma-separated Apollo person ids"),
    item_ids: str = Query(default="", description="Comma-separated finder item ids"),
    db: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
) -> Any:
    """Latest async phone-request state per Apollo person id / finder item id —
    ``{persons: {id: {phone_status, phone, phone_type, dnc_status}}, items: {…}}``.
    The UI polls this while any visible row is PENDING. Tenant-scoped."""
    return await apollo_phone_service.phone_status_for(
        db, person_ids=_csv(person_ids), item_ids=_csv(item_ids),
    )


@router.get("/recruiter-finder/apollo-webhook-events")
async def apollo_webhook_events(
    limit: int = 20,
    db: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
) -> Any:
    """Recent raw deliveries to the Apollo phone webhook — answers "is Apollo actually
    delivering?". Not tenant-scoped (bodies span workspaces), so internal-only."""
    if not is_all_access(getattr(current_user, "tenant_id", None)):
        raise HTTPException(status_code=403, detail="Webhook diagnostics are internal-only.")
    return {"events": await apollo_phone_service.recent_webhook_events(db, limit=limit)}


class ReprocessBody(BaseModel):
    """POST /recruiter-finder/apollo-webhook-events/reprocess — see
    apollo_phone_service.reprocess_events. dry_run defaults ON."""
    dry_run: bool = True
    since: datetime | None = None
    event_ids: list[str] | None = None
    record_charges: bool = True
    resolve_via_apollo: bool = False
    max_resolve: int = Field(default=25, ge=0, le=500)
    include_reprocessed: bool = False


@router.post("/recruiter-finder/apollo-webhook-events/reprocess")
async def reprocess_apollo_webhook_events(
    body: ReprocessBody,
    current_user=Depends(get_current_user),
) -> Any:
    """Backfill logged Apollo phone deliveries that matched no request row (internal-
    only — events span workspaces). Dry run by default; the report masks numbers."""
    if not is_all_access(getattr(current_user, "tenant_id", None)):
        raise HTTPException(status_code=403, detail="Webhook reprocessing is internal-only.")
    return await apollo_phone_service.reprocess_events(
        dry_run=body.dry_run, since=body.since, event_ids=body.event_ids,
        record_charges=body.record_charges, resolve_via_apollo=body.resolve_via_apollo,
        max_resolve=body.max_resolve, include_reprocessed=body.include_reprocessed,
    )


def _remote_addr(request: Request) -> str:
    """Best-effort client IP for webhook logging: first X-Forwarded-For hop, X-Real-IP, then peer.

    Header values are client-supplied, so this is for diagnostics only, not trust decisions.
    """
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return fwd or request.headers.get("x-real-ip") or (request.client.host if request.client else "")


_REF_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def _clean_ref(*candidates: str | None) -> str:
    """The first syntactically valid correlation ref (uuid-ish), else ''. A garbage
    ref is treated as no ref — the delivery then falls back to person-id matching."""
    for c in candidates:
        c = (c or "").strip()
        if c and _REF_RE.match(c):
            return c
    return ""


async def _handle_apollo_phone_webhook(request: Request, token: str | None, ref: str) -> dict:
    """Shared body of both Apollo phone-webhook routes.

    Parses the raw body as JSON (None when empty or invalid) and checks `token` against
    APOLLO_WEBHOOK_TOKEN in constant time; any token passes when no secret is configured. The
    delivery is always logged and, with a valid token, matched to pending phone requests. Raises
    401 after logging when the token is wrong; otherwise returns the processing counts.
    """
    settings = get_settings()
    raw = (await request.body()).decode("utf-8", errors="replace")
    try:
        payload: Any = json.loads(raw) if raw.strip() else None
    except ValueError:
        payload = None
    expected = (settings.apollo_webhook_token or "").strip()
    token_ok = not expected or hmac.compare_digest((token or "").encode(), expected.encode())
    result = await apollo_phone_service.process_webhook(
        payload, ref=ref, token_ok=token_ok, remote_addr=_remote_addr(request), raw_body=raw,
    )
    if not token_ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook token")
    return result


@webhook_router.post("/recruiter-finder/apollo-phone-webhook", status_code=status.HTTP_200_OK)
async def apollo_phone_webhook(
    request: Request,
    token: str | None = Query(default=None, description="Shared webhook secret (APOLLO_WEBHOOK_TOKEN)."),
    ref: str = Query(default="", description="Legacy: correlation ref as a query param."),
) -> dict:
    """Receive Apollo's asynchronous phone delivery (``{people: [{id, phone_numbers: […]}]}``).
    Legacy form (ref in the query string) — kept for requests already in flight. Apollo
    doesn't sign the webhook, so the request is authenticated by our own ``token`` in
    the URL; a bad token is logged as a raw event (diagnostics) and rejected 401. Every
    other delivery returns 200 — even a benign no-match — so Apollo doesn't retry it."""
    return await _handle_apollo_phone_webhook(request, token, _clean_ref(ref))


@webhook_router.post("/recruiter-finder/apollo-phone-webhook/{path_ref}", status_code=status.HTTP_200_OK)
async def apollo_phone_webhook_with_ref(
    path_ref: str,
    request: Request,
    token: str | None = Query(default=None, description="Shared webhook secret (APOLLO_WEBHOOK_TOKEN)."),
    ref: str = Query(default="", description="Ignored when the path carries a ref."),
) -> dict:
    """Same delivery, with the correlation ref as a PATH segment (what ApolloClient now
    sends — Apollo was observed to drop all but the first query param)."""
    return await _handle_apollo_phone_webhook(request, token, _clean_ref(path_ref, ref))
