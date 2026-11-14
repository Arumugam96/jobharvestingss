"""Capture + serve inbound outreach replies (app/models/outreach_reply.py).

The write path is driven by the Brevo inbound-parse webhook: a prospect's reply is
parsed into an OutreachReplyORM row, matched to the outreach it answers (by the
In-Reply-To / References headers against the send's provider_message_id, with a
sender-email fallback), the matched send's ``replied_at`` is stamped, and the reply is
forwarded to the tenant's alert mailbox (the email "alarm").

The read path serves the Mail-logs thread (replies merged with sends), the in-app
notification feed (unread replies for the bell + toast), and the mark-as-read actions.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.tenant_context import apply_tenant, get_current_tenant_id
from app.models.outreach import EmailOutreachORM
from app.models.outreach_reply import OutreachReplyORM
from app.services.email_html_sanitizer import sanitize_email_html
from app.services.email_service import EmailSender

logger = structlog.get_logger(__name__)

# All <angle-bracket> message-ids in a header value (In-Reply-To / References).
_MSGID_RE = re.compile(r"<[^>]+>")
_PREVIEW_LEN = 160
# String(255) columns on OutreachReplyORM — inbound values are truncated to fit.
_SHORT_COL_LEN = 255
_AUTO_REPLY_SUBJECT_PREFIXES = (
    "automatic reply:",
    "out of office:",
    "out of office",
    "auto:",
    "autoreply:",
    "auto-reply:",
    "automatische antwort:",
    "réponse automatique:",
    "risposta automatica:",
    "respuesta automática:",
    "abwesenheitsnotiz:",
)


# ── Inbound payload parsing ──────────────────────────────────────────────────

def _as_items(payload: Any) -> list[dict]:
    """Normalize a Brevo inbound-parse body to a list of message dicts. Brevo posts
    ``{"items": [ {...} ]}``; tolerate a bare object or a top-level list too."""
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return [i for i in items if isinstance(i, dict)]
        return [payload]
    if isinstance(payload, list):
        return [i for i in payload if isinstance(i, dict)]
    return []


def _addr(obj: Any) -> tuple[str, str]:
    """Extract ``(address, name)`` from Brevo's address shape — a dict
    ``{"Address","Name"}`` (or ``{"email","name"}``), or a bare string."""
    if isinstance(obj, dict):
        return (
            (obj.get("Address") or obj.get("email") or obj.get("Email") or "").strip(),
            (obj.get("Name") or obj.get("name") or "").strip(),
        )
    if isinstance(obj, str):
        return (obj.strip(), "")
    return ("", "")


def _first_recipient(item: dict) -> tuple[str, str]:
    """(email, name) of the first "To" entry of an inbound item; ("", "") if absent."""
    to = item.get("To") or item.get("to") or []
    if isinstance(to, list) and to:
        return _addr(to[0])
    return _addr(to)


def _headers(item: dict) -> dict:
    """The item's Headers map (either key casing); {} when missing or not a dict."""
    h = item.get("Headers") or item.get("headers") or {}
    return h if isinstance(h, dict) else {}


def _header_value(item: dict, *names: str) -> str:
    """Case-insensitive lookup of a header across the top-level item and its
    Headers map (Brevo exposes In-Reply-To both as a top-level field and a header)."""
    headers = _headers(item)
    lowered = {str(k).lower(): v for k, v in headers.items()}
    for name in names:
        if name in item and item[name]:
            return str(item[name])
        v = lowered.get(name.lower())
        if v:
            return str(v if not isinstance(v, list) else ", ".join(map(str, v)))
    return ""


def _parse_dt(raw: Any) -> datetime:
    """Parse the reply's sent time (ISO-8601 / RFC-2822), falling back to now()."""
    if isinstance(raw, str) and raw.strip():
        txt = raw.strip()
        try:
            return datetime.fromisoformat(txt.replace("Z", "+00:00"))
        except ValueError:
            pass
        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(txt)
            if dt is not None:
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    return datetime.now(timezone.utc)


def _msgids(*values: str) -> list[str]:
    """All message-ids found across the given header values, normalized to include
    the angle brackets, de-duped in first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        for m in _MSGID_RE.findall(value or ""):
            if m not in seen:
                seen.add(m)
                out.append(m)
        # A header may carry a bare id without brackets — keep it as a candidate too.
        bare = (value or "").strip()
        if bare and "<" not in bare and bare not in seen:
            seen.add(bare)
            out.append(f"<{bare}>")
            out.append(bare)
    return out


def _body_text(item: dict) -> str:
    """Prefer the provider's extracted message (reply without quoted history), then
    the raw text body, then a stripped HTML body."""
    for key in ("ExtractedMarkdownMessage", "RawTextBody", "TextBody", "text"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    html = item.get("RawHtmlBody") or item.get("HtmlBody") or item.get("html") or ""
    if isinstance(html, str) and html.strip():
        return re.sub(r"<[^>]+>", " ", html).strip()
    return ""


def _detect_auto_reply(item: dict) -> bool:
    """Detect if the inbound message is an auto-reply (Out-of-Office, delivery
    notification, auto-responder, etc.) based on RFC 3834 headers, Microsoft-specific
    headers, Precedence, and well-known subject prefixes."""
    # RFC 3834 — the definitive signal: "auto-replied", "auto-generated", etc.
    auto_sub = _header_value(item, "Auto-Submitted", "auto-submitted").lower()
    if auto_sub and auto_sub != "no":
        return True

    # Microsoft Exchange / Outlook auto-response suppression header.
    if _header_value(item, "X-Auto-Response-Suppress", "x-auto-response-suppress"):
        return True

    # Microsoft-specific OOF header.
    if _header_value(
        item, "X-MS-Exchange-Organization-AutoReply", "x-ms-exchange-organization-autoreply"
    ):
        return True

    # Precedence: auto_reply / bulk / junk (common auto-responder convention).
    prec = _header_value(item, "Precedence", "precedence").lower()
    if prec in ("auto_reply", "bulk", "junk"):
        return True

    # Subject-line patterns used by major mail systems.
    subject = str(item.get("Subject") or item.get("subject") or "").strip().lower()
    for prefix in _AUTO_REPLY_SUBJECT_PREFIXES:
        if subject.startswith(prefix):
            return True

    return False


def _clean(value: Any, limit: int | None = None) -> str:
    """Inbound text made safe to store: NUL bytes stripped (Postgres rejects them in
    text) and, when ``limit`` is given, truncated to the column length."""
    text = str(value or "").replace("\x00", "")
    return text[:limit] if limit else text


def _message_id(item: dict) -> str:
    """The item's own Message-ID as stored (stripped, NUL-free, column-truncated)."""
    raw = _header_value(item, "MessageId", "Message-Id", "Message-ID").strip()
    return _clean(raw, _SHORT_COL_LEN)


def _scrub_json(value: Any) -> Any:
    """Copy of a JSON-like payload with NUL bytes stripped from every string."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {_scrub_json(k): _scrub_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_json(v) for v in value]
    return value


def reply_preview(body: str, limit: int = _PREVIEW_LEN) -> str:
    """One-line preview of a reply body for the notification/toast (collapsed
    whitespace, truncated with an ellipsis)."""
    text = re.sub(r"\s+", " ", body or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ── Matching ─────────────────────────────────────────────────────────────────

async def _match_outreach(
    db: AsyncSession, *, in_reply_to: str, references: str, from_email: str,
    received_at: datetime | None = None,
) -> EmailOutreachORM | None:
    """Find the send this reply answers. Primary: the reply's In-Reply-To/References
    carry our stamped Message-ID (EmailOutreachORM.provider_message_id). Fallback:
    the newest SUCCESSFULLY-SENT email to this sender whose send time is at or before
    ``received_at`` (a reply can't answer a later send). When those fallback candidates
    belong to more than one tenant the reply is left unmatched (None) rather than
    guessing. Runs under all-access (the webhook has no tenant), so it can match
    across tenants; the caller adopts the matched row's tenant."""
    candidates = _msgids(in_reply_to, references)
    if candidates:
        row = (
            await db.execute(
                select(EmailOutreachORM)
                .where(EmailOutreachORM.provider_message_id.in_(candidates))
                .order_by(EmailOutreachORM.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None:
            return row

    sender = (from_email or "").strip().lower()
    if sender:
        sent_time = func.coalesce(EmailOutreachORM.sent_at, EmailOutreachORM.created_at)
        conds = [
            func.lower(EmailOutreachORM.to_email) == sender,
            EmailOutreachORM.channel == "email",
            EmailOutreachORM.status == "sent",
        ]
        if received_at is not None:
            when = received_at.astimezone(timezone.utc) if received_at.tzinfo else received_at
            conds.append(sent_time <= when)
        tenants = (
            await db.execute(select(EmailOutreachORM.tenant_id).where(*conds).distinct().limit(2))
        ).scalars().all()
        if len(tenants) > 1:
            logger.info("inbound_reply_sender_match_ambiguous", tenants=len(tenants))
            return None
        row = (
            await db.execute(
                select(EmailOutreachORM)
                .where(*conds)
                .order_by(sent_time.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None:
            return row
    return None


# ── Write path (webhook) ─────────────────────────────────────────────────────

async def record_inbound_replies(
    db: AsyncSession, payload: Any, email_sender: EmailSender | None = None
) -> dict[str, int]:
    """Parse a Brevo inbound-parse batch, store each reply, stamp the matched send's
    ``replied_at``, and forward to the tenant's alert mailbox. Returns
    {received, stored, matched, forwarded, duplicates}.

    Idempotent per Message-ID: an item whose message id is already stored (a Brevo
    retry, a poller overlap) or repeats earlier in the same payload is skipped and
    counted in ``duplicates``. Each item is written inside its own SAVEPOINT, so one
    item's failure (logged and skipped) can't poison the session for the rest. Text is
    stripped of NUL bytes and truncated to the column lengths; ``body_html`` is
    allow-list sanitized before storing.

    COMMITS the session once the batch is written, and only then sends the forward
    alerts (stamping ``forwarded`` in a second commit), so an alert never goes out for
    a reply that wasn't persisted. A failure of that first commit is rolled back and
    re-raised (nothing was stored — the caller should let the provider retry / not
    advance its cursor)."""
    items = _as_items(payload)
    settings = get_settings()
    stored = matched = forwarded = duplicates = 0

    # Message-IDs already captured — the webhook path's dedup (the pollers also
    # pre-filter, this makes every ingestion path idempotent).
    ids = {mid for mid in map(_message_id, items) if mid}
    seen: set[str] = set()
    if ids:
        seen = set(
            (
                await db.execute(
                    select(OutreachReplyORM.message_id).where(OutreachReplyORM.message_id.in_(ids))
                )
            ).scalars().all()
        )

    to_forward: list[tuple[OutreachReplyORM, str]] = []
    for item in items:
        try:
            message_id = _message_id(item) or None
            if message_id is not None and message_id in seen:
                duplicates += 1
                continue

            from_email, from_name = _addr(item.get("From") or item.get("from"))
            to_email, _ = _first_recipient(item)
            subject = str(item.get("Subject") or item.get("subject") or "").strip()
            in_reply_to = _header_value(item, "InReplyTo", "In-Reply-To")
            references = _header_value(item, "References")
            body = _body_text(item)
            received_at = _parse_dt(
                item.get("SentAtDate") or item.get("Date") or _header_value(item, "Date")
            )
            raw_html = item.get("RawHtmlBody") or item.get("HtmlBody") or ""
            body_html = sanitize_email_html(_clean(raw_html)) if isinstance(raw_html, str) else ""

            is_auto = _detect_auto_reply(item)

            async with db.begin_nested():
                send = await _match_outreach(
                    db, in_reply_to=in_reply_to, references=references,
                    from_email=from_email, received_at=received_at,
                )
                tenant_id = send.tenant_id if send is not None else "internal"

                reply = OutreachReplyORM(
                    tenant_id=tenant_id,
                    outreach_id=send.id if send is not None else None,
                    job_id=send.job_id if send is not None else None,
                    recruiter_id=send.recruiter_id if send is not None else None,
                    from_email=_clean(from_email, _SHORT_COL_LEN),
                    from_name=_clean(from_name, _SHORT_COL_LEN),
                    to_email=_clean(to_email, _SHORT_COL_LEN),
                    company=_clean((send.company if send is not None else "") or "",
                                   _SHORT_COL_LEN),
                    subject=_clean(subject),
                    body=_clean(body),
                    body_html=body_html or None,
                    message_id=message_id,
                    in_reply_to=_clean(in_reply_to or references) or None,
                    received_at=received_at,
                    is_auto_reply=is_auto,
                    raw=_scrub_json(item),
                )

                # Denormalized "has reply" flag — auto-replies (OOO etc.) don't
                # count as a real prospect reply, so skip the stamp.
                if send is not None and not is_auto and (
                    send.replied_at is None or received_at > send.replied_at
                ):
                    send.replied_at = received_at

                db.add(reply)
                await db.flush()  # assign reply.id before we build the forward/log
            stored += 1
            if send is not None:
                matched += 1
            if message_id is not None:
                seen.add(message_id)
            to_forward.append((reply, tenant_id))
        except Exception as exc:  # one bad item must not sink the batch
            logger.warning("inbound_reply_item_failed", error=str(exc))
            continue

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    # Alerts go out only for replies that are durably stored.
    for reply, tenant_id in to_forward:
        if await _forward_reply(reply, tenant_id, email_sender, settings):
            reply.forwarded = True
            forwarded += 1
    if forwarded:
        try:
            await db.commit()
        except Exception as exc:  # the replies are stored; only the flag is lost
            await db.rollback()
            logger.warning("inbound_reply_forward_flag_failed", error=str(exc))

    logger.info(
        "inbound_replies_recorded",
        received=len(items), stored=stored, matched=matched, forwarded=forwarded,
        duplicates=duplicates,
    )
    return {
        "received": len(items), "stored": stored, "matched": matched,
        "forwarded": forwarded, "duplicates": duplicates,
    }


async def _forward_reply(
    reply: OutreachReplyORM, tenant_id: str, email_sender: EmailSender | None, settings
) -> bool:
    """Forward a captured reply to the tenant's alert mailbox. Best-effort: a missing
    address or a send failure is logged and returns False (the reply is still stored
    and shown in-app)."""
    to_addr = settings.reply_forward_address(tenant_id)
    if not to_addr:
        logger.info("inbound_reply_forward_skipped", tenant=tenant_id, reason="no_address")
        return False
    if email_sender is None:
        email_sender = EmailSender(settings)

    who = reply.from_name or reply.from_email or "A prospect"
    company = f" · {reply.company}" if reply.company else ""
    subject = f"↩ Reply from {who}{company}: {reply.subject or '(no subject)'}"
    link = ""
    base = (settings.public_base_url or "").rstrip("/")
    if base and reply.outreach_id:
        link = f"\n\nOpen the thread: {base}/mail/{reply.outreach_id}"
    lead = (
        f"{who} replied to your outreach"
        f"{(' for ' + reply.company) if reply.company else ''}.\n"
        f"From: {reply.from_email}\n"
        f"Subject: {reply.subject or '(no subject)'}\n"
        f"{'-' * 40}\n\n"
    )
    body = f"{lead}{reply.body or '(no message body)'}{link}\n"
    try:
        await email_sender.send_email_with_attachments(
            recipients=[to_addr],
            subject=subject,
            body=body,
            # Reply-To the prospect so the team can answer straight from the alert.
            from_email=None,
            reply_to=reply.from_email or None,
        )
        return True
    except Exception as exc:
        logger.warning("inbound_reply_forward_failed", tenant=tenant_id, error=str(exc))
        return False


# ── Read path ────────────────────────────────────────────────────────────────

def reply_to_dict(row: OutreachReplyORM) -> dict[str, Any]:
    """Serialize a reply as an INBOUND thread message — same shape the frontend
    thread renderer consumes for sends, tagged ``direction="inbound"`` so it renders
    as a received message (not a send). The id is prefixed so it can't collide with a
    send-row id in the merged thread. ``body_html`` is re-sanitized on the way out so
    rows stored before sanitize-on-store existed are safe to render too."""
    received = row.received_at or row.created_at
    return {
        "id": f"reply-{row.id}",
        "reply_id": row.id,
        "direction": "inbound",
        "outreach_id": row.outreach_id,
        "job_id": row.job_id,
        "recruiter_id": row.recruiter_id,
        "channel": "email",
        "outreach_kind": "reply",
        "company": row.company,
        "contact_name": row.from_name or None,
        "from_email": row.from_email,
        "to_email": row.to_email,
        "subject": row.subject,
        "body": row.body,
        "body_html": sanitize_email_html(row.body_html) or None,
        "status": "received",
        "is_read": row.is_read,
        "is_auto_reply": row.is_auto_reply,
        "created_at": received.isoformat() if received else None,
        "received_at": row.received_at.isoformat() if row.received_at else None,
    }


async def replies_for_thread(
    db: AsyncSession,
    *,
    job_id: str | None = None,
    recruiter_id: str | None = None,
    outreach_ids: list[str] | None = None,
) -> list[OutreachReplyORM]:
    """Every captured reply belonging to a job/recruiter thread (or to a set of send
    ids), oldest first — merged with the sends to form the two-sided conversation."""
    conds = []
    if job_id:
        conds.append(OutreachReplyORM.job_id == job_id)
    if recruiter_id:
        conds.append(OutreachReplyORM.recruiter_id == recruiter_id)
    if outreach_ids:
        conds.append(OutreachReplyORM.outreach_id.in_([i for i in outreach_ids if i]))
    if not conds:
        return []
    stmt = apply_tenant(select(OutreachReplyORM), OutreachReplyORM.tenant_id).where(or_(*conds))
    stmt = stmt.order_by(OutreachReplyORM.received_at.asc().nullslast(), OutreachReplyORM.created_at.asc())
    return list((await db.execute(stmt)).scalars().all())


async def unread_reply_count(db: AsyncSession) -> int:
    """Number of unread inbound replies in the current tenant (drives the bell badge)."""
    stmt = apply_tenant(select(func.count()).select_from(OutreachReplyORM), OutreachReplyORM.tenant_id)
    stmt = stmt.where(OutreachReplyORM.is_read.is_(False))
    return (await db.execute(stmt)).scalar_one()


async def recent_replies(db: AsyncSession, *, limit: int = 20, unread_only: bool = True) -> list[OutreachReplyORM]:
    """The newest replies for the notification feed (bell + toast)."""
    stmt = apply_tenant(select(OutreachReplyORM), OutreachReplyORM.tenant_id)
    if unread_only:
        stmt = stmt.where(OutreachReplyORM.is_read.is_(False))
    stmt = stmt.order_by(OutreachReplyORM.received_at.desc().nullslast(), OutreachReplyORM.created_at.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


def notification_dict(row: OutreachReplyORM) -> dict[str, Any]:
    """Compact reply shape for the notification bell/toast — enough to render the
    row and deep-link to the thread."""
    received = row.received_at or row.created_at
    return {
        "id": row.id,
        "outreach_id": row.outreach_id,
        "job_id": row.job_id,
        "recruiter_id": row.recruiter_id,
        "from_name": row.from_name,
        "from_email": row.from_email,
        "company": row.company,
        "subject": row.subject,
        "preview": reply_preview(row.body),
        "is_read": row.is_read,
        "is_auto_reply": row.is_auto_reply,
        "received_at": received.isoformat() if received else None,
    }


async def mark_reply_read(db: AsyncSession, reply_id: str, *, read: bool = True) -> bool:
    """Flip one reply's read flag (tenant-scoped). Returns True when a row changed."""
    row = (
        await db.execute(
            apply_tenant(select(OutreachReplyORM), OutreachReplyORM.tenant_id).where(
                OutreachReplyORM.id == reply_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    row.is_read = read
    return True


async def mark_all_replies_read(db: AsyncSession) -> int:
    """Mark every unread reply of the caller's OWN tenant read. Returns the count.

    Scoped to exactly ``get_current_tenant_id()`` even for internal (all-access)
    users, so an internal "Mark all read" never clears client tenants' unread
    replies (those are cleared one by one, via mark_reply_read)."""
    rows = (
        await db.execute(
            select(OutreachReplyORM).where(
                OutreachReplyORM.tenant_id == get_current_tenant_id(),
                OutreachReplyORM.is_read.is_(False),
            )
        )
    ).scalars().all()
    for row in rows:
        row.is_read = True
    return len(rows)
