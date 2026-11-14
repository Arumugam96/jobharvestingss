"""Apollo asynchronous phone discovery for the Contact Finder (/contacts).

Apollo never returns a phone in the /people/match response: it POSTs it minutes
later to the ``webhook_url`` passed with the reveal. This module owns that
round-trip (tables in app/models/apollo_phone.py):

  1. BEFORE the Apollo call the finder inserts a ``pending`` apollo_phone_requests
     row (``create_request``) carrying a fresh ``ref`` that ApolloClient embeds in the
     webhook URL — so a delivery can never beat its row.
  2. Right after the call ``settle_request`` records what Apollo said synchronously
     (person id learned, ``not_found`` when nobody matched, ``rejected`` when Apollo
     refused the webhook_url and the call went out email-only); ``attach_links`` then
     ties the row to the recruiter / finder item / reveal-log row once those exist.
  3. ``process_webhook`` (POST /recruiter-finder/apollo-phone-webhook) logs every raw
     delivery, matches each ``people[]`` entry by ref + Apollo person id, picks the best
     number, charges the tenant's Finder budget ``recruiter_finder_phone_credit_cost``
     credits (ONLY on delivery of a real number) and writes the phone onto every
     linked row. Idempotent: an already found/not_found row is skipped, so Apollo
     retries never double-charge.
  4. ``expire_stale`` (APScheduler, every 10 min) marks rows with no delivery after
     ``apollo_phone_pending_ttl_minutes`` as ``expired``; a late delivery still lands.
     Between ticks, ``phone_status_for`` already reads a past-TTL pending row as
     NOT_FOUND so the UI never waits on the scheduler.

Data-integrity: only a number Apollo actually delivered is ever stored; statuses stay
truthful (PENDING while waiting, NOT_FOUND when Apollo had none / never answered).

The webhook and the expiry job run without a request tenant (ContextVar default =
internal, all-access — same as the Brevo webhooks); the tenant to charge comes from
the matched row.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.tenant_context import apply_tenant, bind_session_tenant
from app.models.apollo_phone import ApolloPhoneRequestORM, ApolloWebhookEventORM
from app.models.harvest_run import ScrapedJobORM
from app.models.recruiter import RecruiterORM
from app.models.recruiter_finder import (
    RecruiterFinderItemORM,
    RecruiterFinderJobORM,
    RecruiterFinderRevealLogORM,
)
from app.services import apollo_budget, recruiter_finder_budget
from app.services import harvest_run_service as _hrs
from app.services.harvest_run_service import db_read, db_write

logger = structlog.get_logger(__name__)

# Request-row statuses.
PENDING = "pending"
FOUND = "found"
NOT_FOUND = "not_found"
EXPIRED = "expired"
REJECTED = "rejected"
CANCELLED = "cancelled"
# A delivery for a row in one of these is a duplicate (Apollo retry) — skipped, no charge.
_FINAL = (FOUND, NOT_FOUND, CANCELLED)

# UI / recruiter phone_status vocabulary.
PS_PENDING = "PENDING"
PS_VERIFIED = "VERIFIED"
PS_PUBLIC = "PUBLIC"
PS_NOT_FOUND = "NOT_FOUND"
PS_BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
# Pre-rename value of PS_BUDGET_EXCEEDED — still on rows stored before the rename.
PS_SKIPPED_BUDGET_LEGACY = "SKIPPED_BUDGET"
PS_UNAVAILABLE = "UNAVAILABLE"

# Request-row purposes — whose budget a delivered phone is charged to.
PURPOSE_FINDER = "finder"          # Contact Finder: tenant Finder budget + global counter
PURPOSE_ENRICHMENT = "enrichment"  # harvest enrichment: global + enrichment sub-cap only

# Namespaces for deterministic refs (idempotent re-creation of synthetic rows).
_UNSOLICITED_NS = uuid.UUID("6f1d3c52-8a0e-4c1e-9b7a-1f3e5a0c2d41")
BACKFILL_NS = uuid.UUID("0b8e7f4a-2d3c-4e5f-8a9b-7c6d5e4f3a21")

_BODY_MAX = 64 * 1024
# Lower rank = preferred.
_TYPE_RANK = {"mobile": 0, "work_direct": 1}


def _now() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


# ── Settings helpers ─────────────────────────────────────────────────────────

def phone_enabled() -> bool:
    """Phone reveal needs a webhook URL for Apollo to deliver to."""
    return bool((get_settings().apollo_webhook_url or "").strip())


def phone_cost() -> int:
    """Credits charged per delivered phone (RECRUITER_FINDER_PHONE_CREDIT_COST, floored at 0)."""
    return max(0, int(get_settings().recruiter_finder_phone_credit_cost))


# ── Budget pre-flight ────────────────────────────────────────────────────────

async def pending_count(tenant_id: str) -> int:
    """Outstanding (pending) phone requests for ``tenant_id`` (explicit tenant filter —
    the budget is per tenant even for the all-access internal workspace)."""
    async def _q(db: AsyncSession) -> int:
        """Count this tenant's pending request rows."""
        stmt = select(func.count()).select_from(ApolloPhoneRequestORM).where(
            ApolloPhoneRequestORM.tenant_id == tenant_id,
            ApolloPhoneRequestORM.status == PENDING,
        )
        return int((await db.execute(stmt)).scalar() or 0)
    return await db_read(_q) or 0


async def pending_phone_credits(tenant_id: str) -> int:
    """Credits the tenant's outstanding phone requests would cost if all delivered."""
    return await pending_count(tenant_id) * phone_cost()


async def pending_phone_credits_for_purpose(purpose: str) -> int:
    """Credits every outstanding (pending) ``purpose`` phone request — all tenants: the
    enrichment sub-cap and the global cap are account-wide — would cost if delivered."""
    async def _q(db: AsyncSession) -> int:
        """Count pending request rows for ``purpose`` across all tenants."""
        stmt = select(func.count()).select_from(ApolloPhoneRequestORM).where(
            ApolloPhoneRequestORM.purpose == purpose,
            ApolloPhoneRequestORM.status == PENDING,
        )
        return int((await db.execute(stmt)).scalar() or 0)
    return (await db_read(_q) or 0) * phone_cost()


def mask_phone(number: str | None) -> str:
    """``+1******0116`` — for logs / reports (never print a delivered number in full)."""
    n = str(number or "")
    if len(n) <= 4:
        return "*" * len(n)
    lead = "+" if n.startswith("+") else ""
    return lead + "*" * (len(n) - 4 - len(lead)) + n[-4:]


async def can_afford_phone(tenant_id: str, cap: int | None, *, extra_cost: int = 0) -> bool:
    """True when today's remaining Finder budget covers one more phone on top of every
    outstanding request (and ``extra_cost`` — e.g. the email credit of the same
    reveal). Unlimited cap / budget read failure → True."""
    remaining = await recruiter_finder_budget.remaining_today(tenant_id, cap=cap)
    if remaining is None:
        return True
    return remaining - await pending_phone_credits(tenant_id) - extra_cost >= phone_cost()


# ── Request lifecycle ────────────────────────────────────────────────────────

async def create_request(
    db: AsyncSession, *, ref: str, tenant_id: str, apollo_person_id: str | None = None,
    requested_by: str | None = None, finder_job_id: str | None = None,
    finder_item_id: str | None = None, purpose: str = PURPOSE_FINDER,
    source: str = "contact_finder", recruiter_id: str | None = None,
    linkedin_url: str | None = None, scraped_job_id: str | None = None,
) -> ApolloPhoneRequestORM:
    """Insert one ``pending`` row (call BEFORE the Apollo request). With
    ``recruiter_id`` the recruiter is marked phone_status PENDING in the same
    transaction — only when it has no phone yet (a scraped / delivered number and its
    VERIFIED / PUBLIC status are never downgraded)."""
    row = ApolloPhoneRequestORM(
        id=str(uuid.uuid4()), ref=ref, tenant_id=tenant_id,
        apollo_person_id=apollo_person_id or None, requested_by=requested_by,
        finder_job_id=finder_job_id, finder_item_id=finder_item_id,
        recruiter_id=recruiter_id or None, linkedin_url=(linkedin_url or None),
        scraped_job_id=scraped_job_id or None,
        purpose=(purpose or PURPOSE_FINDER)[:20], source=(source or "")[:40],
        status=PENDING, requested_at=_now(),
    )
    db.add(row)
    if recruiter_id:
        R = RecruiterORM
        await db.execute(
            update(R).where(
                R.id == recruiter_id,
                func.coalesce(R.contact_number, "") == "",
                R.phone_status.notin_((PS_VERIFIED, PS_PUBLIC)),
            ).values(phone_status=PS_PENDING)
        )
    await db.flush()
    return row


async def begin_request(
    *, tenant_id: str, apollo_person_id: str | None = None, requested_by: str | None = None,
    finder_job_id: str | None = None, finder_item_id: str | None = None,
    purpose: str = PURPOSE_FINDER, source: str = "contact_finder",
    recruiter_id: str | None = None, linkedin_url: str | None = None,
    scraped_job_id: str | None = None,
) -> str:
    """Create a pending row in its own committed session and return its ``ref`` ('' on
    a DB failure — the caller then requests no phone rather than one it can't track)."""
    ref = str(uuid.uuid4())
    row = await db_write(lambda db: create_request(
        db, ref=ref, tenant_id=tenant_id, apollo_person_id=apollo_person_id,
        requested_by=requested_by, finder_job_id=finder_job_id, finder_item_id=finder_item_id,
        purpose=purpose, source=source, recruiter_id=recruiter_id, linkedin_url=linkedin_url,
        scraped_job_id=scraped_job_id,
    ))
    return ref if row is not None else ""


def status_after_call(result: Any) -> str:
    """UI phone_status implied by the synchronous Apollo answer."""
    if result is None or not getattr(result, "matched", False):
        return PS_NOT_FOUND
    if not getattr(result, "phone_requested", False):
        return PS_UNAVAILABLE
    return PS_PENDING


async def _rows_for_ref(db: AsyncSession, ref: str) -> list[ApolloPhoneRequestORM]:
    """Every request row carrying ``ref`` (normally one; empty list if none)."""
    stmt = select(ApolloPhoneRequestORM).where(ApolloPhoneRequestORM.ref == ref)
    return list((await db.execute(stmt)).scalars().all())


async def settle_request(db: AsyncSession, ref: str, result: Any) -> str:
    """Record the synchronous Apollo answer on the ref's row(s): learn the person id,
    mark ``not_found`` (nobody matched / the call failed) or ``rejected`` (no phone
    request went out). Returns the UI phone_status."""
    ps = status_after_call(result)
    for row in await _rows_for_ref(db, ref):
        if row.status != PENDING:
            continue  # a (very) early delivery already resolved it
        pid = getattr(result, "id", None) if result is not None else None
        if pid and not row.apollo_person_id:
            row.apollo_person_id = str(pid)
        if ps == PS_NOT_FOUND:
            row.status = NOT_FOUND
        elif ps == PS_UNAVAILABLE:
            row.status = REJECTED
        if row.status != PENDING and row.recruiter_id:
            # Linked up front (harvest enrichment): resolve the recruiter's PENDING
            # marker now — no delivery will follow.
            await _apply_to_links(db, row, only={"recruiter_id": row.recruiter_id})
    await db.flush()
    return ps


async def cancel_request(db: AsyncSession, ref: str) -> None:
    """The finder dropped this contact (e.g. budget ran out before persisting it):
    stop tracking — a later delivery is logged but neither charged nor stored."""
    for row in await _rows_for_ref(db, ref):
        if row.status == PENDING:
            row.status = CANCELLED
    await db.flush()


async def attach_links(
    db: AsyncSession, ref: str, *, recruiter_id: str | None = None,
    finder_item_id: str | None = None, finder_job_id: str | None = None,
    reveal_log_id: str | None = None, scraped_job_id: str | None = None,
) -> None:
    """Tie the ref's row(s) to the rows a delivered phone must update. If the delivery
    already happened (race), apply the phone + its credits to the new links now."""
    for row in await _rows_for_ref(db, ref):
        new_links = {}
        for field, value in (("recruiter_id", recruiter_id), ("finder_item_id", finder_item_id),
                             ("finder_job_id", finder_job_id), ("reveal_log_id", reveal_log_id),
                             ("scraped_job_id", scraped_job_id)):
            if value and not getattr(row, field):
                setattr(row, field, value)
                new_links[field] = value
        if row.status == FOUND and new_links:
            await _apply_to_links(db, row, only=new_links)
    await db.flush()


# ── Webhook ──────────────────────────────────────────────────────────────────

def pick_best_number(phone_numbers: Any) -> dict | None:
    """Best deliverable number from Apollo's ``phone_numbers[]``: drop entries with no
    number or explicitly invalid; prefer status_cd=valid_number, then type mobile >
    work_direct > other, then Apollo's own ``position``."""
    if not isinstance(phone_numbers, list):
        return None
    cands = []
    for pn in phone_numbers:
        if not isinstance(pn, dict):
            continue
        number = str(pn.get("sanitized_number") or pn.get("raw_number") or "").strip()
        status_cd = str(pn.get("status_cd") or "").lower()
        if not number or status_cd.startswith("invalid"):
            continue
        type_cd = str(pn.get("type_cd") or "").lower()
        try:
            position = int(pn.get("position") or 0)
        except (TypeError, ValueError):
            position = 0
        cands.append(((0 if status_cd == "valid_number" else 1, _TYPE_RANK.get(type_cd, 2), position),
                      {"number": number[:50], "type": type_cd[:30],
                       "status": PS_VERIFIED if status_cd == "valid_number" else PS_PUBLIC,
                       "dnc": str(pn.get("dnc_status_cd") or "")[:30]}))
    if not cands:
        return None
    return min(cands, key=lambda c: c[0])[1]


async def _recent_found_row(
    db: AsyncSession, person_id: str, since: datetime,
) -> ApolloPhoneRequestORM | None:
    """A FOUND row for ``person_id`` delivered at/after ``since`` — the phone is
    already in hand (and charged), so a repeat delivery must not create/charge another."""
    R = ApolloPhoneRequestORM
    return (await db.execute(
        select(R).where(R.apollo_person_id == person_id, R.status == FOUND, R.delivered_at >= since)
        .order_by(R.delivered_at.desc()).limit(1)
    )).scalar_one_or_none()


async def _recruiter_by_person_id(db: AsyncSession, person_id: str) -> RecruiterORM | None:
    """Most recently seen recruiter with this Apollo person id, across all tenants."""
    R = RecruiterORM
    return (await db.execute(
        select(R).where(R.apollo_person_id == person_id).order_by(R.last_seen_at.desc()).limit(1)
    )).scalar_one_or_none()


async def _unsolicited_row(
    db: AsyncSession, person_id: str, received_at: datetime,
) -> ApolloPhoneRequestORM | None:
    """No ref and no open request, but a harvest recruiter carries this Apollo person
    id (an enrichment call made before refs were tracked): create the request row on
    the fly so the delivery lands on that recruiter. Deterministic ref → a retry inside
    the same minute finds the same row (and a FOUND row from the last day is reused by
    the caller) — never a second charge."""
    rec = await _recruiter_by_person_id(db, person_id)
    if rec is None:
        return None
    ref = str(uuid.uuid5(_UNSOLICITED_NS, f"unsolicited:{person_id}:{received_at:%Y%m%d%H%M}"))
    existing = await _rows_for_ref(db, ref)
    if existing:
        return existing[0]
    row = ApolloPhoneRequestORM(
        id=str(uuid.uuid4()), ref=ref, tenant_id=rec.tenant_id or "internal",
        apollo_person_id=person_id, recruiter_id=rec.id,
        linkedin_url=rec.linkedin_profile_url or None,
        purpose=PURPOSE_ENRICHMENT, source="unsolicited", status=PENDING, requested_at=received_at,
    )
    db.add(row)
    await db.flush()
    logger.info("apollo_phone_unsolicited_row_created", ref=ref, person_id=person_id,
                recruiter_id=rec.id, tenant_id=row.tenant_id)
    return row


async def _find_row(db: AsyncSession, ref: str, person_id: str) -> ApolloPhoneRequestORM | None:
    """Locate the request row a delivered ``people[]`` entry belongs to.

    With a ref: the ref's row for this person id, else the ref's single row whose person
    id was never learned (otherwise None). Without a ref: the newest open row for the
    person, then a FOUND row delivered in the last day (a duplicate), then a synthetic
    row for a harvest recruiter carrying the id (which may INSERT a new row).
    """
    R = ApolloPhoneRequestORM
    if ref:
        if person_id:
            row = (await db.execute(
                select(R).where(R.ref == ref, R.apollo_person_id == person_id)
                .order_by(R.requested_at.desc()).limit(1)
            )).scalar_one_or_none()
            if row is not None:
                return row
        rows = await _rows_for_ref(db, ref)
        # Fallback: the ref's single row whose person id we never learned.
        if len(rows) == 1 and not rows[0].apollo_person_id:
            return rows[0]
        return None
    if person_id:
        # No ref (a hand-registered webhook URL / Apollo dropped it): the newest open
        # request for the person.
        row = (await db.execute(
            select(R).where(R.apollo_person_id == person_id, R.status.notin_(_FINAL))
            .order_by(R.requested_at.desc()).limit(1)
        )).scalar_one_or_none()
        if row is not None:
            return row
        now = _now()
        # Already delivered recently → hand back that FOUND row (counted as a duplicate).
        recent = await _recent_found_row(db, person_id, now - timedelta(days=1))
        if recent is not None:
            return recent
        # Third fallback: a recruiter we enriched carries this Apollo person id.
        return await _unsolicited_row(db, person_id, now)
    return None


async def _resolve_recruiter_by_url(db: AsyncSession, row: ApolloPhoneRequestORM) -> str | None:
    """The recruiter whose (normalized) LinkedIn URL is the row's — within the row's
    tenant. Used when the row was created before its recruiter existed."""
    from app.services.recruiter_service import normalize_linkedin_url

    url = normalize_linkedin_url(row.linkedin_url or "")
    if not url:
        return None
    R = RecruiterORM
    return (await db.execute(
        select(R.id).where(R.linkedin_profile_url == url, R.tenant_id == row.tenant_id)
        .order_by(R.last_seen_at.desc()).limit(1)
    )).scalar_one_or_none()


async def _apply_to_recruiter(db: AsyncSession, row: ApolloPhoneRequestORM, rid: str, found: bool) -> None:
    """Write a phone outcome onto recruiter ``rid``.

    Finder rows overwrite the recruiter's number; enrichment rows only fill an empty
    one. Any remaining PENDING marker then becomes PUBLIC (has a phone) or NOT_FOUND.
    A found enrichment phone also back-fills the person id and re-links the
    recruiter's jobs by LinkedIn URL.
    """
    R = RecruiterORM
    finder = (row.purpose or PURPOSE_FINDER) == PURPOSE_FINDER
    if found and finder:
        # Contact Finder: the user explicitly asked for this person's phone — unchanged.
        await db.execute(update(R).where(R.id == rid)
                         .values(contact_number=row.phone, phone_status=row.phone_status))
    elif found:
        # Harvest enrichment: fill an EMPTY phone only — a scraped number is never
        # overwritten by an Apollo delivery.
        await db.execute(
            update(R).where(R.id == rid, func.coalesce(R.contact_number, "") == "")
            .values(contact_number=row.phone, phone_status=row.phone_status,
                    enrichment_source="apollo")
        )
    # Not delivered — or delivered to a recruiter that already had a number: only its
    # PENDING marker is resolved (PUBLIC when it keeps a phone, else NOT_FOUND).
    await db.execute(
        update(R).where(R.id == rid, R.phone_status == PS_PENDING)
        .values(phone_status=case((func.coalesce(R.contact_number, "") != "", PS_PUBLIC),
                                  else_=PS_NOT_FOUND))
    )
    if found and not finder:
        rec = await db.get(R, rid)
        if rec is not None:
            if row.apollo_person_id and not rec.apollo_person_id:
                rec.apollo_person_id = row.apollo_person_id
            if rec.linkedin_profile_url:
                from app.services.recruiter_service import link_recruiter_jobs_by_url
                await link_recruiter_jobs_by_url(db, rid, rec.linkedin_profile_url)


async def _apply_to_links(db: AsyncSession, row: ApolloPhoneRequestORM, *, only: dict | None = None) -> None:
    """Write the row's outcome onto its linked recruiter / finder item / reveal log /
    job. Found → the number + status (for an enrichment row only onto a recruiter
    with no phone yet — a scraped number is never overwritten), and the charged
    credits onto the item's job and the reveal log. Not delivered → NOT_FOUND, but a
    recruiter keeps any phone it already had (only its PENDING marker is resolved).
    ``only`` restricts to newly attached links (late attach after delivery). A row
    with no recruiter but a LinkedIn URL first resolves (and links) it by URL."""
    if only is None and not row.recruiter_id and row.linkedin_url:
        rid = await _resolve_recruiter_by_url(db, row)
        if rid:
            row.recruiter_id = rid
    links = only if only is not None else {
        "recruiter_id": row.recruiter_id, "finder_item_id": row.finder_item_id,
        "finder_job_id": row.finder_job_id, "reveal_log_id": row.reveal_log_id,
        "scraped_job_id": row.scraped_job_id,
    }
    found = row.status == FOUND and bool(row.phone)
    credits = row.credits_charged if found else 0

    if links.get("recruiter_id"):
        await _apply_to_recruiter(db, row, links["recruiter_id"], found)
    if links.get("finder_item_id"):
        I = RecruiterFinderItemORM
        if found:
            await db.execute(update(I).where(I.id == links["finder_item_id"])
                             .values(phone=row.phone, phone_status=row.phone_status))
        else:
            await db.execute(update(I).where(I.id == links["finder_item_id"], I.phone_status == PS_PENDING)
                             .values(phone_status=PS_NOT_FOUND))
    if links.get("finder_job_id") and credits:
        J = RecruiterFinderJobORM
        await db.execute(update(J).where(J.id == links["finder_job_id"])
                         .values(credits_spent=J.credits_spent + credits))
    if links.get("reveal_log_id"):
        L = RecruiterFinderRevealLogORM
        if found:
            await db.execute(update(L).where(L.id == links["reveal_log_id"])
                             .values(phone=row.phone, phone_status=row.phone_status,
                                     credits_spent=L.credits_spent + credits))
        else:
            await db.execute(update(L).where(L.id == links["reveal_log_id"], L.phone_status == PS_PENDING)
                             .values(phone_status=PS_NOT_FOUND))
    if links.get("scraped_job_id") and found:
        await db.execute(
            update(ScrapedJobORM)
            .where(ScrapedJobORM.id == links["scraped_job_id"],
                   func.coalesce(ScrapedJobORM.contact_number, "") == "")
            .values(contact_number=row.phone)
        )


async def _charge_delivery(
    db: AsyncSession, row: ApolloPhoneRequestORM, cost: int, *, day, endpoint: str,
    ledger_at: datetime | None = None,
) -> None:
    """Book an already-incurred phone charge (unconditional adds, no cap check).
    finder → the tenant's Finder budget + the global counter + ledger; enrichment →
    ONLY the global counter + its purpose counter + ledger, never the Finder budget.
    ``ledger_at`` back-dates the ledger row (the backfill books on the event's date)."""
    if cost <= 0:
        return
    purpose = row.purpose or PURPOSE_FINDER
    if purpose == PURPOSE_FINDER:
        await recruiter_finder_budget._charge(db, day, row.tenant_id, cost)
    if ledger_at is None:
        # A delivered phone is a real Apollo charge on the shared account — count it on
        # the global counter (+ purpose row) and the ledger too (already incurred, so
        # an unconditional add).
        await apollo_budget._settle(db, day, 0, cost, purpose, endpoint)
        return
    from app.models.harvest_run import ApolloCreditLedgerORM

    await apollo_budget._reserve(db, day, 0, cost)  # cap 0 = unconditional bump
    await apollo_budget._reserve_purpose(db, day, purpose, 0, cost)
    db.add(ApolloCreditLedgerORM(created_at=ledger_at, purpose=purpose,
                                 endpoint=endpoint[:60], credits=cost))


def _settle_row_delivery(row: ApolloPhoneRequestORM, best: dict | None, cost: int,
                         delivered_at: datetime) -> None:
    """Mark the row FOUND with the best number and ``cost`` charged, else NOT_FOUND.

    Only mutates the ORM row; the charge itself is booked separately (_charge_delivery).
    """
    row.delivered_at = delivered_at
    if best:
        row.status = FOUND
        row.phone = best["number"]
        row.phone_type = best["type"]
        row.phone_status = best["status"]
        row.dnc_status = best["dnc"]
        row.credits_charged = cost
    else:
        row.status = NOT_FOUND


def _people(payload: Any) -> list[dict]:
    """Person dicts from a webhook payload: ``people[]``, a single ``person``, or a bare list.

    Returns [] for any other shape; non-dict entries are dropped.
    """
    if isinstance(payload, dict):
        people = payload.get("people")
        if isinstance(people, list):
            return [p for p in people if isinstance(p, dict)]
        if isinstance(payload.get("person"), dict):  # tolerate a single-person shape
            return [payload["person"]]
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    return []


def _body_text(payload: Any, raw: str | None) -> str:
    """Webhook body to store on the event log: the raw text, else JSON, capped at 64 KB."""
    if raw is not None:
        return raw[:_BODY_MAX]
    try:
        return json.dumps(payload, default=str)[:_BODY_MAX]
    except Exception:
        return str(payload)[:_BODY_MAX]


async def process_delivery(
    db: AsyncSession, payload: Any, *, ref: str, token_ok: bool, remote_addr: str = "",
    raw_body: str | None = None,
) -> dict:
    """Session-taking core of ``process_webhook`` (one transaction: the event log row,
    the request rows, the linked rows and the budget charge commit together)."""
    people = _people(payload)
    counts = {"received": len(people), "matched": 0, "found": 0, "not_found": 0, "duplicates": 0}
    cost = phone_cost()
    if token_ok:
        today = _now().date()
        for person in people:
            pid = str(person.get("id") or "").strip()
            row = await _find_row(db, ref, pid)
            if row is None:
                continue
            counts["matched"] += 1
            if row.status in _FINAL:
                counts["duplicates"] += 1  # Apollo retry / already resolved — never re-charge
                continue
            if pid and not row.apollo_person_id:
                row.apollo_person_id = pid
            best = pick_best_number(person.get("phone_numbers"))
            _settle_row_delivery(row, best, cost, _now())
            if best:
                await _charge_delivery(db, row, cost, day=today, endpoint="phone_webhook")
                counts["found"] += 1
            else:
                counts["not_found"] += 1
            await db.flush()
            await _apply_to_links(db, row)
            logger.info(
                "apollo_phone_delivered", ref=row.ref, request_id=row.id, tenant_id=row.tenant_id,
                person_id=row.apollo_person_id, found=bool(best), credits=row.credits_charged,
                purpose=row.purpose, source=row.source, phone=mask_phone(row.phone),
            )
    db.add(ApolloWebhookEventORM(
        id=str(uuid.uuid4()), received_at=_now(), ref=(ref or "")[:64],
        remote_addr=(remote_addr or "")[:64], token_ok=token_ok,
        people_count=len(people), matched_count=counts["matched"], found_count=counts["found"],
        body=_body_text(payload, raw_body),
    ))
    await db.flush()
    return counts


async def process_webhook(
    payload: Any, *, ref: str = "", token_ok: bool = True, remote_addr: str = "",
    raw_body: str | None = None,
) -> dict:
    """Handle one Apollo phone delivery. Always logs an apollo_webhook_events row; with a
    valid token, resolves the matching request rows. Never raises — returns counts
    (all zero when the DB write failed)."""
    logger.info("apollo_phone_webhook_received", ref=ref, token_ok=token_ok, remote_addr=remote_addr,
                people=len(_people(payload)))
    result = await db_write(lambda db: process_delivery(
        db, payload, ref=ref, token_ok=token_ok, remote_addr=remote_addr, raw_body=raw_body,
    ))
    return result or {"received": len(_people(payload)), "matched": 0, "found": 0,
                      "not_found": 0, "duplicates": 0}


# ── Expiry ───────────────────────────────────────────────────────────────────

async def expire_pending(db: AsyncSession, *, older_than: datetime) -> int:
    """Mark pending rows requested before ``older_than`` as expired; return how many.

    Each row's links are resolved as not delivered (PENDING markers → NOT_FOUND).
    Flushes but does not commit.
    """
    rows = (await db.execute(
        select(ApolloPhoneRequestORM).where(
            ApolloPhoneRequestORM.status == PENDING,
            ApolloPhoneRequestORM.requested_at < older_than,
        )
    )).scalars().all()
    for row in rows:
        row.status = EXPIRED
        await _apply_to_links(db, row)
    await db.flush()
    return len(rows)


async def expire_stale() -> int:
    """APScheduler tick: pending rows older than the TTL → expired (linked statuses →
    NOT_FOUND). A late delivery still lands and is charged. Never raises."""
    ttl = max(1, int(get_settings().apollo_phone_pending_ttl_minutes))
    n = await db_write(lambda db: expire_pending(db, older_than=_now() - timedelta(minutes=ttl))) or 0
    if n:
        logger.info("apollo_phone_requests_expired", count=n, ttl_minutes=ttl)
    return n


# ── Read models (polling / diagnostics) ──────────────────────────────────────

def _aware(dt: datetime | None) -> datetime | None:
    """Re-attach UTC to a naive datetime (as read back from SQLite); None passes through."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)  # SQLite drops the tz — stored as UTC
    return dt


def _pending_cutoff() -> datetime:
    """Requests made before this are past the pending TTL."""
    ttl = max(1, int(get_settings().apollo_phone_pending_ttl_minutes))
    return _now() - timedelta(minutes=ttl)


def ui_phone_status(row: ApolloPhoneRequestORM, *, stale_before: datetime | None = None) -> str:
    """UI phone status for a request row. With ``stale_before``, a ``pending`` row
    requested before it reads as NOT_FOUND — the expiry job only runs every 10 min, so
    this closes the gap at read time (the row itself is expired by ``expire_stale``)."""
    if row.status == PENDING:
        requested = _aware(row.requested_at)
        if stale_before is not None and requested is not None and requested < stale_before:
            return PS_NOT_FOUND
        return PS_PENDING
    if row.status == FOUND:
        return row.phone_status or PS_PUBLIC
    if row.status == REJECTED:
        return PS_UNAVAILABLE
    return PS_NOT_FOUND


def _row_view(row: ApolloPhoneRequestORM, *, stale_before: datetime | None = None) -> dict:
    """JSON-ready polling view of a request row: UI status, number, type, DNC, requested_at."""
    requested = _aware(row.requested_at)
    return {"phone_status": ui_phone_status(row, stale_before=stale_before),
            "phone": row.phone or "",
            "phone_type": row.phone_type or "", "dnc_status": row.dnc_status or "",
            "requested_at": requested.isoformat() if requested else None}


async def phone_status_for(
    db: AsyncSession, *, person_ids: list[str] | None = None, item_ids: list[str] | None = None,
) -> dict:
    """Latest phone request per Apollo person id / finder item id (tenant-scoped). A
    pending request older than apollo_phone_pending_ttl_minutes reads as NOT_FOUND."""
    R = ApolloPhoneRequestORM
    stale_before = _pending_cutoff()
    out: dict[str, dict] = {"persons": {}, "items": {}}
    person_ids = [p for p in (person_ids or []) if p][:200]
    item_ids = [i for i in (item_ids or []) if i][:200]
    if person_ids:
        rows = (await db.execute(apply_tenant(
            select(R).where(R.apollo_person_id.in_(person_ids)).order_by(R.requested_at.asc()), R.tenant_id,
        ))).scalars().all()
        for r in rows:  # ascending → the newest request per person wins
            out["persons"][r.apollo_person_id] = _row_view(r, stale_before=stale_before)
    if item_ids:
        rows = (await db.execute(apply_tenant(
            select(R).where(R.finder_item_id.in_(item_ids)).order_by(R.requested_at.asc()), R.tenant_id,
        ))).scalars().all()
        for r in rows:
            out["items"][r.finder_item_id] = _row_view(r, stale_before=stale_before)
    return out


async def job_pending_count(db: AsyncSession, job_id: str) -> int:
    """Phone requests of a Finder job still awaiting delivery (not tenant-scoped)."""
    R = ApolloPhoneRequestORM
    stmt = select(func.count()).select_from(R).where(R.finder_job_id == job_id, R.status == PENDING)
    return int((await db.execute(stmt)).scalar() or 0)


async def recent_webhook_events(db: AsyncSession, *, limit: int = 20) -> list[dict]:
    """Newest logged webhook deliveries (diagnostics), ``limit`` clamped to 1-100.

    Not tenant-scoped; each body is truncated to 4000 characters.
    """
    E = ApolloWebhookEventORM
    rows = (await db.execute(
        select(E).order_by(E.received_at.desc()).limit(max(1, min(100, limit)))
    )).scalars().all()
    return [{
        "id": e.id, "received_at": e.received_at.isoformat() if e.received_at else None,
        "ref": e.ref, "remote_addr": e.remote_addr, "token_ok": e.token_ok,
        "people_count": e.people_count, "matched_count": e.matched_count,
        "found_count": e.found_count, "body": e.body[:4000],
    } for e in rows]


# ── Backfill: reprocess logged deliveries that matched no request row ────────

_REOPEN = (PENDING, EXPIRED, REJECTED)


def _event_day(received_at: datetime):
    """UTC calendar date of a delivery: the budget day a backfilled charge is booked on."""
    return received_at.astimezone(timezone.utc).date()


async def _resolve_target(
    db: AsyncSession, pid: str, person: dict, state: dict, *, dry_run: bool,
    resolve_via_apollo: bool,
) -> dict | None:
    """Who a delivered phone belongs to, by EXACT identifiers only (never by timestamp
    proximity): (i) recruiters.apollo_person_id, (ii) a finder item / reveal-log row
    with this person id, (iii) the LinkedIn URL in the body → recruiter, (iv) opt-in:
    ask Apollo for the person's LinkedIn URL (costs a lookup — capped) → (iii)."""
    from app.services.recruiter_service import normalize_linkedin_url

    rec = await _recruiter_by_person_id(db, pid)
    if rec is not None:
        return {"via": "recruiter_person_id", "tenant_id": rec.tenant_id, "recruiter_id": rec.id,
                "linkedin_url": rec.linkedin_profile_url}
    I = RecruiterFinderItemORM
    item = (await db.execute(
        select(I).where(I.apollo_person_id == pid).order_by(I.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if item is not None:
        return {"via": "finder_item", "tenant_id": item.tenant_id, "recruiter_id": item.recruiter_id,
                "finder_item_id": item.id, "linkedin_url": item.linkedin_url or None}
    L = RecruiterFinderRevealLogORM
    log = (await db.execute(
        select(L).where(L.person_id == pid).order_by(L.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if log is not None:
        return {"via": "reveal_log", "tenant_id": log.tenant_id, "recruiter_id": log.recruiter_id,
                "reveal_log_id": log.id, "linkedin_url": log.linkedin_url or None}

    async def _by_url(url: str, via: str) -> dict | None:
        """Target for the newest recruiter with this normalized LinkedIn URL, or None."""
        norm = normalize_linkedin_url(url or "")
        if not norm:
            return None
        R = RecruiterORM
        r = (await db.execute(
            select(R).where(R.linkedin_profile_url == norm).order_by(R.last_seen_at.desc()).limit(1)
        )).scalar_one_or_none()
        if r is None:
            return None
        return {"via": via, "tenant_id": r.tenant_id, "recruiter_id": r.id, "linkedin_url": norm}

    hit = await _by_url(str(person.get("linkedin_url") or ""), "body_linkedin_url")
    if hit is not None:
        return hit
    if resolve_via_apollo:
        if state["resolved_via_apollo"] >= state["max_resolve"]:
            state["resolve_capped"] += 1
            return None
        if dry_run:
            state["would_resolve_via_apollo"] += 1
            return None
        from app.services.apollo_client import ApolloAPIError, ApolloClient

        state["resolved_via_apollo"] += 1
        try:
            res = await ApolloClient(get_settings(), purpose=PURPOSE_ENRICHMENT).match_person_by_id(
                pid, reveal_email=False, reveal_phone=False,
            )
        except ApolloAPIError as exc:
            logger.warning("apollo_phone_backfill_resolve_failed", person_id=pid, error=str(exc))
            return None
        if res is not None and res.matched and res.linkedin_url:
            return await _by_url(res.linkedin_url, "apollo_lookup")
    return None


async def _reprocess_event(
    db: AsyncSession, ev: ApolloWebhookEventORM, state: dict, *, dry_run: bool,
    record_charges: bool, resolve_via_apollo: bool,
) -> dict:
    """Session-taking core of ``reprocess_events`` for ONE logged delivery (the caller
    commits — or rolls back for a dry run). Never writes a number Apollo didn't deliver;
    charges are booked on the EVENT's UTC date (back-dated ledger), purpose enrichment
    (never the Finder budget) unless an open Finder request is what got the phone."""
    R = ApolloPhoneRequestORM
    out: dict = {"event_id": ev.id, "received_at": None, "people": [], "applied": 0, "found": 0,
                 "charged": 0, "error": ""}
    received = _aware(ev.received_at) or _now()
    out["received_at"] = received.isoformat()
    try:
        payload = json.loads(ev.body or "")
    except (ValueError, TypeError) as exc:
        out["error"] = f"body_parse_failed: {exc}"[:200]
        return out
    cost = phone_cost()
    day = _event_day(received)
    for person in _people(payload):
        pid = str(person.get("id") or "").strip()
        best = pick_best_number(person.get("phone_numbers"))
        entry = {"person_id": pid, "phone": mask_phone(best["number"]) if best else "",
                 "phone_status": best["status"] if best else "", "outcome": "", "via": "",
                 "tenant_id": "", "recruiter_id": None, "charged": 0}
        out["people"].append(entry)
        if not pid:
            entry["outcome"] = "skipped_no_person_id"
            continue
        ref = str(uuid.uuid5(BACKFILL_NS, f"{ev.id}:{pid}"))
        if await _rows_for_ref(db, ref):
            entry["outcome"] = "skipped_already_backfilled"
            continue
        if await _recent_found_row(db, pid, received - timedelta(days=1)) is not None:
            entry["outcome"] = "skipped_already_found"
            continue

        row = (await db.execute(
            select(R).where(R.apollo_person_id == pid, R.status.in_(_REOPEN),
                            R.requested_at <= received)
            .order_by(R.requested_at.desc()).limit(1)
        )).scalar_one_or_none()
        if row is not None:
            entry["via"] = "open_request"
        else:
            target = await _resolve_target(db, pid, person, state, dry_run=dry_run,
                                           resolve_via_apollo=resolve_via_apollo)
            if target is None and not best:
                entry["outcome"] = "skipped_unresolved_no_number"
                continue
            target = target or {"via": "unresolved"}
            entry["via"] = target["via"]
            row = ApolloPhoneRequestORM(
                id=str(uuid.uuid4()), ref=ref, tenant_id=target.get("tenant_id") or "internal",
                apollo_person_id=pid, recruiter_id=target.get("recruiter_id"),
                finder_item_id=target.get("finder_item_id"), reveal_log_id=target.get("reveal_log_id"),
                linkedin_url=target.get("linkedin_url") or (str(person.get("linkedin_url") or "") or None),
                purpose=PURPOSE_ENRICHMENT, source="backfill", status=PENDING, requested_at=received,
            )
            db.add(row)

        _settle_row_delivery(row, best, cost, received)
        if best and record_charges and cost:
            await _charge_delivery(db, row, cost, day=day, endpoint="phone_webhook_backfill",
                                   ledger_at=received)
            entry["charged"] = cost
            out["charged"] += cost
        await db.flush()
        await _apply_to_links(db, row)
        entry["outcome"] = ("found" if best else "not_found") + (
            "_unlinked" if not (row.recruiter_id or row.finder_item_id or row.reveal_log_id) else ""
        )
        entry["tenant_id"] = row.tenant_id
        entry["recruiter_id"] = row.recruiter_id
        out["applied"] += 1
        out["found"] += 1 if best else 0

    ev.reprocessed_at = _now()
    ev.matched_count = min(int(ev.people_count or 0), int(ev.matched_count or 0) + out["applied"])
    ev.found_count = int(ev.found_count or 0) + out["found"]
    note = {"applied": out["applied"], "found": out["found"], "charged": out["charged"],
            "people": [{k: e[k] for k in ("person_id", "outcome", "via", "phone", "charged")}
                       for e in out["people"]]}
    ev.reprocess_note = json.dumps(note)[:8000]
    await db.flush()
    return out


async def reprocess_events(
    *, dry_run: bool = True, since: datetime | None = None, event_ids: list[str] | None = None,
    record_charges: bool = True, resolve_via_apollo: bool = False, max_resolve: int = 25,
    include_reprocessed: bool = False, limit: int = 500,
) -> dict:
    """Backfill logged Apollo deliveries (apollo_webhook_events) that matched no request
    row — the harvest enrichment path used to ask for phones without creating one.
    Only token-authenticated events with ``matched_count < people_count``; already
    reprocessed events are skipped unless ``event_ids`` / ``include_reprocessed``.
    One transaction per event; ``dry_run`` (the default) runs the identical logic and
    rolls it back. Phones are MASKED in the returned report."""
    E = ApolloWebhookEventORM
    state = {"max_resolve": max(0, int(max_resolve)), "resolved_via_apollo": 0,
             "would_resolve_via_apollo": 0, "resolve_capped": 0}
    report: dict = {"dry_run": dry_run, "record_charges": record_charges,
                    "resolve_via_apollo": resolve_via_apollo, "events": [], "errors": [],
                    "totals": {"events": 0, "people": 0, "applied": 0, "found": 0,
                               "charged": 0, "unlinked": 0}}
    factory = _hrs.get_session_factory(get_settings())
    async with factory() as db:
        stmt = select(E.id).where(E.token_ok.is_(True), E.matched_count < E.people_count)
        if event_ids:
            stmt = select(E.id).where(E.id.in_(list(event_ids)), E.token_ok.is_(True))
        else:
            if not include_reprocessed:
                stmt = stmt.where(E.reprocessed_at.is_(None))
            if since is not None:
                stmt = stmt.where(E.received_at >= since)
        ids = [i for (i,) in (await db.execute(stmt.order_by(E.received_at.asc())
                                                .limit(max(1, int(limit))))).all()]
    for eid in ids:
        async with factory() as db:
            await bind_session_tenant(db)
            try:
                ev = await db.get(E, eid)
                if ev is None:
                    continue
                res = await _reprocess_event(db, ev, state, dry_run=dry_run,
                                             record_charges=record_charges,
                                             resolve_via_apollo=resolve_via_apollo)
                if dry_run:
                    await db.rollback()
                else:
                    await db.commit()
            except Exception as exc:  # one bad event never stops the backfill
                await db.rollback()
                logger.warning("apollo_phone_backfill_event_failed", event_id=eid, error=str(exc))
                report["errors"].append({"event_id": eid, "error": str(exc)[:300]})
                continue
        report["events"].append(res)
        t = report["totals"]
        t["events"] += 1
        t["people"] += len(res["people"])
        t["applied"] += res["applied"]
        t["found"] += res["found"]
        t["charged"] += res["charged"]
        t["unlinked"] += sum(1 for p in res["people"] if p["outcome"].endswith("_unlinked"))
        if res["error"]:
            report["errors"].append({"event_id": eid, "error": res["error"]})
    report["apollo_lookups"] = {k: state[k] for k in
                                ("resolved_via_apollo", "would_resolve_via_apollo", "resolve_capped")}
    report["unresolved"] = [
        {"event_id": e["event_id"], "received_at": e["received_at"], **p}
        for e in report["events"] for p in e["people"] if p["outcome"].endswith("_unlinked")
    ]
    logger.info("apollo_phone_backfill_done", dry_run=dry_run, **report["totals"],
                errors=len(report["errors"]))
    return report
