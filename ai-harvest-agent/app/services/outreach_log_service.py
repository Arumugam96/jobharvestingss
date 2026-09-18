"""Read/query helpers over the `email_outreach` send log (app/models/outreach.py).

The send log is written by app/routes/outreach_routes.py on every send attempt.
These helpers turn it into the source of truth for:
  * whether a job/recruiter has already been contacted (icon state, dedup guard)
  * the ordered outreach thread (initial + follow-ups + linkedin) for a job
  * the recent-outreach list shown on the Outreach page
  * the prior message that seeds a follow-up draft

All reads filter to `status == "sent"` where "has this been sent?" is the
question; the raw thread/history reads return every attempt (sent + failed) so
the UI can show delivery outcomes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.harvest_run import ScrapedJobORM
from app.models.outreach import EmailOutreachORM
from app.models.recruiter import RecruiterORM


def outreach_to_dict(row: EmailOutreachORM, contact_name: str | None = None) -> dict[str, Any]:
    """Serialize one send-log row for the API (matches the frontend's expectations).

    ``contact_name`` (the recruiter/poster name, resolved by the caller via
    contact_names_for_rows) is folded in for the Mail-logs Contact column — the
    send row itself doesn't store it. Delivery-engagement fields carry the Mailjet
    event state; they are None until a webhook advances them (or for LinkedIn rows)."""
    return {
        "id": row.id,
        "job_id": row.job_id,
        "recruiter_id": row.recruiter_id,
        "channel": row.channel,
        "outreach_kind": row.outreach_kind,
        "parent_outreach_id": row.parent_outreach_id,
        "provider_message_id": row.provider_message_id,
        "company": row.company,
        "client_type": row.client_type,
        "tone": row.tone,
        "contact_name": contact_name,
        "to_email": row.to_email,
        "from_email": row.from_email,
        "subject": row.subject,
        "body": row.body,
        "llm_generated": row.llm_generated,
        "fallback_used": row.fallback_used,
        "status": row.status,
        "error_message": row.error_message,
        "delivery_status": row.delivery_status,
        "delivered_at": row.delivered_at.isoformat() if row.delivered_at else None,
        "opened_at": row.opened_at.isoformat() if row.opened_at else None,
        "bounced_at": row.bounced_at.isoformat() if row.bounced_at else None,
        "replied_at": row.replied_at.isoformat() if row.replied_at else None,
        "events": row.events or [],
        "sent_by": row.sent_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def contact_names_for_rows(
    db: AsyncSession, rows: list[EmailOutreachORM]
) -> dict[str, str]:
    """Map job_id → contact (poster/recruiter) name for a batch of outreach rows via
    a single lookup on scraped_jobs (recruiter eager-loaded). Jobs with no resolvable
    name are omitted. Used to fold contact_name into the Mail-logs list without
    storing it on the send row."""
    job_ids = {r.job_id for r in rows if r.job_id}
    if not job_ids:
        return {}
    jobs = (
        await db.execute(select(ScrapedJobORM).where(ScrapedJobORM.id.in_(job_ids)))
    ).scalars().all()
    names: dict[str, str] = {}
    for job in jobs:
        name = (job.job_poster_name or "").strip()
        if not name and job.recruiter is not None:
            name = (job.recruiter.person_name or "").strip()
        if name:
            names[job.id] = name
    return names


# ── Mailjet delivery-event application ───────────────────────────────────────────
# Mailjet's "sent" event = accepted by the recipient's mail server (= delivered);
# there is no separate "delivered" event. Positive progression never downgrades and
# never overrides a terminal bounce/blocked/spam.
_NEG_STATUS = {"bounce": "bounced", "blocked": "blocked", "spam": "spam"}
_POS_RANK = {"delivered": 1, "opened": 2, "clicked": 3}

# Brevo (transactional webhook) event names → the internal vocab _apply_delivery_event
# understands (which was modelled on Mailjet's). Brevo's "delivered" maps to our "sent"
# (both mean "accepted by the recipient server"); proxy opens (Apple Mail Privacy et al.)
# count as opens; hard/soft bounce and invalid_email all land as a negative "bounce".
# Names with no entry (request/deferred/error/sent) are ignored by record_brevo_events.
BREVO_EVENT_MAP = {
    "delivered": "sent",
    "opened": "open",
    "unique_opened": "open",
    "first_opening": "open",   # Brevo's first-open event (fires once per recipient)
    "firstopening": "open",
    "proxy_open": "open",
    "unique_proxy_open": "open",
    "click": "click",
    "hard_bounce": "bounce",
    "soft_bounce": "bounce",
    "invalid_email": "bounce",
    "blocked": "blocked",
    "spam": "spam",
    "unsubscribed": "unsub",
}


def _event_time(raw: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _apply_delivery_event(
    row: EmailOutreachORM, event: str | None, occurred_at: datetime, url: str | None = None
) -> bool:
    """Mutate one send row for a single Mailjet event; return True if it changed.

    Records EVERY event in the ordered `events` trail (so the UI can show every
    status the mail hit) and advances the headline `delivery_status`/timestamps for
    the events that represent forward progress. When the event carries a `url` (a
    click's target link), it's stored on the trail entry so the detail page can show
    exactly what the recipient clicked."""
    ev = (event or "").strip().lower()
    if not ev:
        return False
    # Append to the ordered trail first — even events that don't move the headline
    # (an open after a click, a duplicate sent, a spam). Reassign (not list.append)
    # so SQLAlchemy detects the JSON mutation and flushes it.
    entry = {"event": ev, "at": occurred_at.isoformat()}
    link = (url or "").strip()
    if link:
        entry["url"] = link
    row.events = [*(row.events or []), entry]
    changed = True
    current = row.delivery_status
    if ev == "unsub":
        # Unsubscribe is surfaced as its own terminal headline; the actual
        # suppression (DB write) happens in record_delivery_events, which has the
        # session. Don't let it be overwritten by a stray later positive event.
        if current != "unsubscribed":
            row.delivery_status = "unsubscribed"
        return changed
    if ev in _NEG_STATUS:
        if row.bounced_at is None:
            row.bounced_at = occurred_at
            changed = True
        if current != _NEG_STATUS[ev]:
            row.delivery_status = _NEG_STATUS[ev]
            changed = True
        return changed
    if current in _NEG_STATUS.values() or current == "unsubscribed":
        return changed  # terminal state — ignore any late positive event
    if ev == "sent":
        if row.delivered_at is None:
            row.delivered_at = occurred_at
            changed = True
        if _POS_RANK.get(current, 0) < _POS_RANK["delivered"]:
            row.delivery_status = "delivered"
            changed = True
    elif ev == "open":
        if row.opened_at is None:
            row.opened_at = occurred_at
            changed = True
        if _POS_RANK.get(current, 0) < _POS_RANK["opened"]:
            row.delivery_status = "opened"
            changed = True
    elif ev == "click":
        if row.opened_at is None:
            row.opened_at = occurred_at  # a click implies an open
            changed = True
        if _POS_RANK.get(current, 0) < _POS_RANK["clicked"]:
            row.delivery_status = "clicked"
            changed = True
    return changed


async def record_delivery_events(db: AsyncSession, events: list[dict], source: str = "mailjet_webhook") -> int:
    """Apply a batch of provider event objects to their outreach rows — matched by the
    CustomID we set at send time (= the row id). Returns the number of rows changed.
    ``source`` labels any resulting suppression (the Brevo path passes "brevo_webhook").

    An ``unsub`` event is routed through the suppression store (keyed by the event's
    email, so it suppresses globally even if the exact send row can't be matched);
    every other event advances the matched row's delivery state + event trail."""
    # Imported lazily to avoid an import cycle (suppression_service → models only,
    # but this keeps the module graph clean since the webhook path is the only user).
    from app.services.suppression_service import add_suppression

    updated = 0
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        custom_id = ev.get("CustomID")
        ev_name = (ev.get("event") or "").strip().lower()

        if ev_name == "unsub":
            email = ev.get("email")
            if not email and custom_id:  # fall back to the matched row's recipient
                row = await get_by_id(db, str(custom_id))
                email = row.to_email if row else None
            if email:
                await add_suppression(db, email=email, source=source, raw_payload=ev)
                updated += 1
            continue  # add_suppression already stamps the row(s); skip the generic path

        if not custom_id:
            continue
        row = await get_by_id(db, str(custom_id))
        if row is None:
            continue
        if _apply_delivery_event(row, ev.get("event"), _event_time(ev.get("time")), url=ev.get("url")):
            updated += 1
    return updated


async def record_brevo_events(db: AsyncSession, events: list[dict]) -> int:
    """Apply a batch of Brevo transactional-webhook events to their outreach rows, reusing
    the Mailjet path. Each Brevo event is normalized to the internal shape
    record_delivery_events expects — CustomID from the ``X-Mailin-custom`` header we set at
    send time (= the row id), the event name mapped via BREVO_EVENT_MAP, the timestamp from
    ``ts_event``/``ts``, and the recipient email — then delegated, so matching-by-id, the
    terminal-state guards, the events trail, and unsub→suppression are all shared with the
    Mailjet path. Brevo events with no internal mapping (request/deferred/error/sent) are
    dropped. Returns the number of rows changed."""
    normalized: list[dict] = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        internal = BREVO_EVENT_MAP.get((ev.get("event") or "").strip().lower())
        if internal is None:
            continue  # unmapped Brevo event (request/deferred/error/…) — nothing to record
        normalized.append({
            "CustomID": ev.get("X-Mailin-custom") or ev.get("x-mailin-custom"),
            "event": internal,
            "time": ev.get("ts_event") or ev.get("ts"),
            "email": ev.get("email"),
            # Brevo puts the clicked target on the "link" field (Mailjet uses "url");
            # carry it through so a click entry records exactly what was clicked.
            "url": ev.get("link") or ev.get("URL") or ev.get("url"),
        })
    if not normalized:
        return 0
    return await record_delivery_events(db, normalized, source="brevo_webhook")


async def sent_status_for_jobs(db: AsyncSession, job_ids: list[str]) -> dict[str, dict]:
    """Latest successful send per job, split by channel — powers the row icons.

    Returns { job_id: { "email": {...} | absent, "linkedin": {...} | absent } }
    where each channel entry is the most-recent sent row's summary plus a
    per-channel followup_count. Only jobs with at least one sent row appear.
    """
    ids = [j for j in {*(job_ids or [])} if j]
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(EmailOutreachORM)
            .where(
                EmailOutreachORM.job_id.in_(ids),
                EmailOutreachORM.status == "sent",
            )
            .order_by(EmailOutreachORM.created_at.asc())
        )
    ).scalars().all()

    out: dict[str, dict] = {}
    for row in rows:  # ascending → last write per (job, channel) wins as "latest"
        job_bucket = out.setdefault(row.job_id, {})
        channel = row.channel or "email"
        entry = job_bucket.get(channel)
        if entry is None:
            entry = {
                "status": "sent",
                "last_sent_at": None,
                "outreach_id": None,
                "outreach_kind": "initial",
                "followup_count": 0,
            }
            job_bucket[channel] = entry
        entry["last_sent_at"] = row.created_at.isoformat() if row.created_at else None
        entry["outreach_id"] = row.id
        entry["outreach_kind"] = row.outreach_kind or "initial"
        if (row.outreach_kind or "") == "followup":
            entry["followup_count"] += 1
    return out


async def thread_messages(
    db: AsyncSession,
    *,
    job_id: str | None = None,
    recruiter_id: str | None = None,
    limit: int = 200,
) -> list[EmailOutreachORM]:
    """Every send attempt (sent + failed) for a job or recruiter, oldest first —
    the outreach thread/history for that contact."""
    if not job_id and not recruiter_id:
        return []
    stmt = select(EmailOutreachORM)
    if job_id:
        stmt = stmt.where(EmailOutreachORM.job_id == job_id)
    if recruiter_id:
        stmt = stmt.where(EmailOutreachORM.recruiter_id == recruiter_id)
    stmt = stmt.order_by(EmailOutreachORM.created_at.asc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


def _parse_day(raw: str | None, *, end: bool = False) -> datetime | None:
    """Parse a 'YYYY-MM-DD' filter value to a UTC datetime — start-of-day, or the
    start of the NEXT day when `end` (so `created_at < end` is inclusive)."""
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(str(raw)[:10]).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return d + timedelta(days=1) if end else d


def _apply_list_filters(stmt, *, search=None, company=None, date_from=None, date_to=None):
    """Apply the Mail-logs list filters to a base `select(EmailOutreachORM)`:
      * `search` (point of contact) → joined poster/recruiter name OR recipient email
      * `company` → EmailOutreachORM.company (real column)
      * `date_from`/`date_to` → created_at range (end is inclusive)
    Outerjoins to scraped_jobs/recruiters are 1:1 on the PK, so no row multiplication."""
    if search:
        like = f"%{search.strip()}%"
        stmt = (
            stmt.outerjoin(ScrapedJobORM, EmailOutreachORM.job_id == ScrapedJobORM.id)
            .outerjoin(RecruiterORM, ScrapedJobORM.recruiter_id == RecruiterORM.id)
            .where(
                or_(
                    func.coalesce(
                        func.nullif(ScrapedJobORM.job_poster_name, ""), RecruiterORM.person_name
                    ).ilike(like),
                    EmailOutreachORM.to_email.ilike(like),
                )
            )
        )
    if company:
        stmt = stmt.where(EmailOutreachORM.company.ilike(f"%{company.strip()}%"))
    df = _parse_day(date_from)
    dt = _parse_day(date_to, end=True)
    if df is not None:
        stmt = stmt.where(EmailOutreachORM.created_at >= df)
    if dt is not None:
        stmt = stmt.where(EmailOutreachORM.created_at < dt)
    return stmt


async def recent_outreach(
    db: AsyncSession,
    *,
    search: str | None = None,
    company: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = 100,
) -> tuple[list[EmailOutreachORM], int]:
    """Server-side paginated + filtered recent-outreach list for the Mail logs page.
    Returns `(rows, total)` where `total` is the whole filtered count (across pages),
    mirroring HarvestRunService.list_scraped_jobs."""
    base = _apply_list_filters(
        select(EmailOutreachORM), search=search, company=company, date_from=date_from, date_to=date_to
    )
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    stmt = (
        base.order_by(EmailOutreachORM.created_at.desc())
        .offset(max(0, (page - 1) * page_size))
        .limit(page_size)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return rows, total


async def outreach_list_stats(
    db: AsyncSession,
    *,
    search: str | None = None,
    company: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, int]:
    """Whole-dataset engagement counts over the *filtered* Mail-logs set (so the stat
    panel stays accurate under pagination). Each count is a subquery over the shared
    filtered base; the UI shows every count as a percentage of `sent`.

      * sent         — successfully dispatched (status=='sent'); the "emails delivered"
                       headline AND the denominator for every percentage
      * failed       — send rejected by the relay at send time (status=='failed')
      * opened       — a recorded open (opened_at set; a click implies an open). Brevo's
                       'opened'/'first_opening' both feed this.
      * clicked      — headline delivery_status 'clicked'
      * unsubscribed — headline 'unsubscribed'
      * blocked      — headline 'blocked'
      * bounced      — headline 'bounced' (soft/hard/invalid collapse here; shown as
                       "Soft bounce" since only soft bounces are enabled)
    """
    base = _apply_list_filters(
        select(EmailOutreachORM), search=search, company=company, date_from=date_from, date_to=date_to
    )

    async def _count(stmt) -> int:
        return (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()

    async def _status(value: str) -> int:
        return await _count(base.where(EmailOutreachORM.delivery_status == value))

    return {
        "sent": await _count(base.where(EmailOutreachORM.status == "sent")),
        "failed": await _count(base.where(EmailOutreachORM.status == "failed")),
        "opened": await _count(base.where(EmailOutreachORM.opened_at.isnot(None))),
        "clicked": await _status("clicked"),
        "unsubscribed": await _status("unsubscribed"),
        "blocked": await _status("blocked"),
        "bounced": await _status("bounced"),
    }


async def latest_sent_email(
    db: AsyncSession,
    *,
    job_id: str | None = None,
    recruiter_id: str | None = None,
) -> EmailOutreachORM | None:
    """Most-recent successfully-sent EMAIL for a job/recruiter — the prior
    message a follow-up draft is built from."""
    if not job_id and not recruiter_id:
        return None
    stmt = select(EmailOutreachORM).where(
        EmailOutreachORM.channel == "email",
        EmailOutreachORM.status == "sent",
    )
    if job_id:
        stmt = stmt.where(EmailOutreachORM.job_id == job_id)
    if recruiter_id:
        stmt = stmt.where(EmailOutreachORM.recruiter_id == recruiter_id)
    stmt = stmt.order_by(EmailOutreachORM.created_at.desc()).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


async def initial_email_sent(
    db: AsyncSession,
    *,
    job_id: str | None = None,
    recruiter_id: str | None = None,
) -> EmailOutreachORM | None:
    """The first successfully-sent INITIAL email for a job/recruiter, if any —
    the duplicate-send guard for initial outreach."""
    if not job_id and not recruiter_id:
        return None
    stmt = select(EmailOutreachORM).where(
        EmailOutreachORM.channel == "email",
        EmailOutreachORM.outreach_kind == "initial",
        EmailOutreachORM.status == "sent",
    )
    if job_id:
        stmt = stmt.where(EmailOutreachORM.job_id == job_id)
    if recruiter_id:
        stmt = stmt.where(EmailOutreachORM.recruiter_id == recruiter_id)
    stmt = stmt.order_by(EmailOutreachORM.created_at.asc()).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_by_id(db: AsyncSession, outreach_id: str) -> EmailOutreachORM | None:
    return (
        await db.execute(select(EmailOutreachORM).where(EmailOutreachORM.id == outreach_id))
    ).scalar_one_or_none()


def build_email_outreach_row(
    *,
    id: str,
    job_id: str | None,
    recruiter_id: str | None,
    provider_message_id: str | None,
    company: str,
    client_type: str,
    tone: str,
    to_email: str,
    from_email: str,
    subject: str,
    body: str,
    fallback_used: bool,
    status: str,
    error_message: str | None,
    sent_by: str,
    outreach_kind: str = "initial",
    parent_outreach_id: str | None = None,
    sent_at: datetime | None = None,
) -> EmailOutreachORM:
    """Construct an EmailOutreachORM send-log row for an EMAIL outreach send — the
    single source of truth for the row shape shared by the manual send route
    (outreach_routes.send_email) and the automated end-of-harvest sweep
    (auto_outreach_service), so the two writers can't drift.

    Seeds the optimistic delivery lifecycle exactly as the manual route did: a
    successful hand-off to Mailjet (``status == "sent"`` — send_email_with_attachments
    raises otherwise) counts as delivered immediately, so the row shows "Delivered"
    without waiting on a webhook; the event webhook only advances it further
    (opened/clicked) or to a terminal negative (bounced/blocked/spam). A failed send
    leaves the delivery fields NULL."""
    delivered = status == "sent"
    when = sent_at or datetime.now(timezone.utc)
    return EmailOutreachORM(
        id=id,
        job_id=job_id,
        recruiter_id=recruiter_id,
        channel="email",
        outreach_kind=outreach_kind,
        parent_outreach_id=parent_outreach_id,
        provider_message_id=provider_message_id,
        company=company,
        client_type=client_type,
        tone=tone or "",
        to_email=to_email,
        from_email=from_email,
        subject=subject,
        body=body,
        attachment_name="",
        llm_generated=not fallback_used,
        fallback_used=fallback_used,
        status=status,
        error_message=error_message,
        delivery_status="delivered" if delivered else None,
        delivered_at=when if delivered else None,
        sent_by=sent_by,
    )
