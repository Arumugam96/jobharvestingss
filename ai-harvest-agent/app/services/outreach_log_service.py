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

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.harvest_run import ScrapedJobORM
from app.models.outreach import EmailOutreachORM


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


def _event_time(raw: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _apply_delivery_event(row: EmailOutreachORM, event: str | None, occurred_at: datetime) -> bool:
    """Mutate one send row for a single Mailjet event; return True if it changed."""
    ev = (event or "").strip().lower()
    changed = False
    current = row.delivery_status
    if ev in _NEG_STATUS:
        if row.bounced_at is None:
            row.bounced_at = occurred_at
            changed = True
        if current != _NEG_STATUS[ev]:
            row.delivery_status = _NEG_STATUS[ev]
            changed = True
        return changed
    if current in _NEG_STATUS.values():
        return changed  # terminal negative state — ignore any late positive event
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


async def record_delivery_events(db: AsyncSession, events: list[dict]) -> int:
    """Apply a batch of Mailjet event objects to their outreach rows — matched by the
    CustomID we set at send time (= the row id). Returns the number of rows changed."""
    updated = 0
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        custom_id = ev.get("CustomID")
        if not custom_id:
            continue
        row = await get_by_id(db, str(custom_id))
        if row is None:
            continue
        if _apply_delivery_event(row, ev.get("event"), _event_time(ev.get("time"))):
            updated += 1
    return updated


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


async def recent_outreach(db: AsyncSession, limit: int = 100) -> list[EmailOutreachORM]:
    """Most-recent outreach across all contacts — the Outreach page list."""
    stmt = (
        select(EmailOutreachORM)
        .order_by(EmailOutreachORM.created_at.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


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
