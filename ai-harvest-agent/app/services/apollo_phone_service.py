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
from app.core.tenant_context import apply_tenant
from app.models.apollo_phone import ApolloPhoneRequestORM, ApolloWebhookEventORM
from app.models.recruiter import RecruiterORM
from app.models.recruiter_finder import (
    RecruiterFinderItemORM,
    RecruiterFinderJobORM,
    RecruiterFinderRevealLogORM,
)
from app.services import recruiter_finder_budget
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
PS_SKIPPED_BUDGET = "SKIPPED_BUDGET"
PS_UNAVAILABLE = "UNAVAILABLE"

_BODY_MAX = 64 * 1024
# Lower rank = preferred.
_TYPE_RANK = {"mobile": 0, "work_direct": 1}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Settings helpers ─────────────────────────────────────────────────────────

def phone_enabled() -> bool:
    """Phone reveal needs a webhook URL for Apollo to deliver to."""
    return bool((get_settings().apollo_webhook_url or "").strip())


def phone_cost() -> int:
    return max(0, int(get_settings().recruiter_finder_phone_credit_cost))


# ── Budget pre-flight ────────────────────────────────────────────────────────

async def pending_count(tenant_id: str) -> int:
    """Outstanding (pending) phone requests for ``tenant_id`` (explicit tenant filter —
    the budget is per tenant even for the all-access internal workspace)."""
    async def _q(db: AsyncSession) -> int:
        stmt = select(func.count()).select_from(ApolloPhoneRequestORM).where(
            ApolloPhoneRequestORM.tenant_id == tenant_id,
            ApolloPhoneRequestORM.status == PENDING,
        )
        return int((await db.execute(stmt)).scalar() or 0)
    return await db_read(_q) or 0


async def pending_phone_credits(tenant_id: str) -> int:
    """Credits the tenant's outstanding phone requests would cost if all delivered."""
    return await pending_count(tenant_id) * phone_cost()


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
    finder_item_id: str | None = None,
) -> ApolloPhoneRequestORM:
    """Insert one ``pending`` row (call BEFORE the Apollo request)."""
    row = ApolloPhoneRequestORM(
        id=str(uuid.uuid4()), ref=ref, tenant_id=tenant_id,
        apollo_person_id=apollo_person_id or None, requested_by=requested_by,
        finder_job_id=finder_job_id, finder_item_id=finder_item_id,
        status=PENDING, requested_at=_now(),
    )
    db.add(row)
    await db.flush()
    return row


async def begin_request(
    *, tenant_id: str, apollo_person_id: str | None = None, requested_by: str | None = None,
    finder_job_id: str | None = None, finder_item_id: str | None = None,
) -> str:
    """Create a pending row in its own committed session and return its ``ref`` ('' on
    a DB failure — the caller then requests no phone rather than one it can't track)."""
    ref = str(uuid.uuid4())
    row = await db_write(lambda db: create_request(
        db, ref=ref, tenant_id=tenant_id, apollo_person_id=apollo_person_id,
        requested_by=requested_by, finder_job_id=finder_job_id, finder_item_id=finder_item_id,
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
    reveal_log_id: str | None = None,
) -> None:
    """Tie the ref's row(s) to the rows a delivered phone must update. If the delivery
    already happened (race), apply the phone + its credits to the new links now."""
    for row in await _rows_for_ref(db, ref):
        new_links = {}
        for field, value in (("recruiter_id", recruiter_id), ("finder_item_id", finder_item_id),
                             ("finder_job_id", finder_job_id), ("reveal_log_id", reveal_log_id)):
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


async def _find_row(db: AsyncSession, ref: str, person_id: str) -> ApolloPhoneRequestORM | None:
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
        # No ref (e.g. a hand-registered webhook URL): the newest open request for the person.
        return (await db.execute(
            select(R).where(R.apollo_person_id == person_id, R.status.notin_(_FINAL))
            .order_by(R.requested_at.desc()).limit(1)
        )).scalar_one_or_none()
    return None


async def _apply_to_links(db: AsyncSession, row: ApolloPhoneRequestORM, *, only: dict | None = None) -> None:
    """Write the row's outcome onto its linked recruiter / finder item / reveal log /
    job. Found → the number + status, and the charged credits onto the item's job and
    the reveal log. Not delivered → NOT_FOUND, but a recruiter keeps any phone it
    already had (only its PENDING marker is resolved). ``only`` restricts to newly
    attached links (late attach after delivery)."""
    links = only if only is not None else {
        "recruiter_id": row.recruiter_id, "finder_item_id": row.finder_item_id,
        "finder_job_id": row.finder_job_id, "reveal_log_id": row.reveal_log_id,
    }
    found = row.status == FOUND and bool(row.phone)
    credits = row.credits_charged if found else 0

    if links.get("recruiter_id"):
        R = RecruiterORM
        if found:
            await db.execute(update(R).where(R.id == links["recruiter_id"])
                             .values(contact_number=row.phone, phone_status=row.phone_status))
        else:
            await db.execute(
                update(R).where(R.id == links["recruiter_id"], R.phone_status == PS_PENDING)
                .values(phone_status=case((R.contact_number != "", PS_PUBLIC), else_=PS_NOT_FOUND))
            )
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


def _people(payload: Any) -> list[dict]:
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
            row.delivered_at = _now()
            if best:
                row.status = FOUND
                row.phone = best["number"]
                row.phone_type = best["type"]
                row.phone_status = best["status"]
                row.dnc_status = best["dnc"]
                row.credits_charged = cost
                if cost:
                    await recruiter_finder_budget._charge(db, today, row.tenant_id, cost)
                counts["found"] += 1
            else:
                row.status = NOT_FOUND
                counts["not_found"] += 1
            await db.flush()
            await _apply_to_links(db, row)
            logger.info(
                "apollo_phone_delivered", ref=row.ref, request_id=row.id, tenant_id=row.tenant_id,
                person_id=row.apollo_person_id, found=bool(best), credits=row.credits_charged,
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
    R = ApolloPhoneRequestORM
    stmt = select(func.count()).select_from(R).where(R.finder_job_id == job_id, R.status == PENDING)
    return int((await db.execute(stmt)).scalar() or 0)


async def recent_webhook_events(db: AsyncSession, *, limit: int = 20) -> list[dict]:
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
