"""Outreach suppression (do-not-contact) — email-keyed store, guard, and tokens.

`email_suppressions` (app/models/suppression.py) is the source of truth checked
before every outreach send (see app/routes/outreach_routes.py::send_email). Keyed on
the normalized email because many sends carry only a job_id with no linked recruiter,
and Mailjet's unsub event / the unsubscribe link both identify the person by email.
When a suppressed email resolves to a recruiter, a mirror flag is set for CRM
visibility, and the recent outreach rows to that address are stamped
`delivery_status="unsubscribed"` so the Mail logs UI shows it.

A TERMINAL delivery failure (hard bounce / ESP block, from the delivery-event
webhook) also lands here — via suppress_bounced() with reason "hard_bounce"/"blocked"
— so a dead address is never retried. That path is intentionally leaner than an
unsubscribe: it only adds the do-not-contact row, leaving the recruiter flag and the
"bounced"/"blocked" delivery status untouched. Soft bounces and spam do not suppress.

A "do_not_call" row (phone_verification_service: a contact asked on a logged call not
to be approached again) is the one TENANT-scoped kind — suppress_do_not_call writes it
under the calling job's tenant, and is_suppressed honours it only for that tenant (an
all-access/internal context honours every tenant's). Unsubscribes and terminal
bounces stay global: the link token / provider event identify only the address, and
a dead or opted-out address is a property of the address itself.

Unsubscribe links carry a stateless HMAC token of the email (no per-send storage);
this module signs and verifies them.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any, Iterable

import structlog
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.contact_normalize import normalize_email
from app.core.tenant_context import get_current_tenant_id, is_all_access
from app.models.outreach import EmailOutreachORM
from app.models.recruiter import RecruiterORM
from app.models.suppression import EmailSuppressionORM

logger = structlog.get_logger(__name__)
_warned_fallback_secret = False

# Reasons whose row applies only to its own tenant (see the module docstring).
DO_NOT_CALL_REASON = "do_not_call"
TENANT_SCOPED_REASONS = frozenset({DO_NOT_CALL_REASON})


def _global_row():
    """SQL: a suppression that applies to every tenant (unsubscribe / bounce / block)."""
    return EmailSuppressionORM.reason.not_in(TENANT_SCOPED_REASONS)


def _scoped_row(tenant_id: str | None):
    """SQL: a tenant-scoped suppression that applies to ``tenant_id`` (the current
    context's tenant when None) — any tenant's for an all-access context."""
    tid = tenant_id or get_current_tenant_id()
    cond = EmailSuppressionORM.reason.in_(TENANT_SCOPED_REASONS)
    return cond if is_all_access(tid) else and_(cond, EmailSuppressionORM.tenant_id == tid)


def _norm(email: str | None) -> str:
    """Normalize to a consistent lowercase key (emails are case-insensitive in
    practice; we key on lowercase everywhere)."""
    cleaned = normalize_email(email) or (email or "")
    return cleaned.strip().lower()


# ── Stateless unsubscribe token (HMAC of the email) ─────────────────────────────

def _secret() -> bytes:
    """HMAC key for unsubscribe tokens: the JWT secret, or "change-me" when unset.

    The fallback is kept (existing tokens keep verifying) but logs a warning the
    first time it is used in this process, since anyone can forge such tokens."""
    global _warned_fallback_secret
    secret = get_settings().jwt_secret_key
    if not secret:
        if not _warned_fallback_secret:
            _warned_fallback_secret = True
            logger.warning(
                "unsubscribe_secret_fallback",
                message="JWT_SECRET_KEY is empty — unsubscribe tokens are signed with the "
                        "public fallback 'change-me'; set JWT_SECRET_KEY",
            )
        secret = "change-me"
    return secret.encode("utf-8")


def _b64(raw: bytes) -> str:
    """URL-safe base64 of ``raw`` with the "=" padding stripped."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    """Inverse of _b64: re-pad and decode (raises on malformed input)."""
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_unsubscribe_token(email: str) -> str:
    """Sign the normalized email into a url-safe token for an unsubscribe link.
    Stateless — the same email always yields a verifiable token, no DB row needed.
    Format: ``b64url(email).b64url(hmac_sha256(email))``."""
    e = _norm(email)
    mac = hmac.new(_secret(), e.encode("utf-8"), hashlib.sha256).digest()
    return f"{_b64(e.encode('utf-8'))}.{_b64(mac)}"


def verify_unsubscribe_token(token: str | None) -> str | None:
    """Return the email if the token's HMAC signature is valid, else None."""
    try:
        e_b64, mac_b64 = (token or "").split(".", 1)
        email = _unb64(e_b64).decode("utf-8")
        expected = hmac.new(_secret(), email.encode("utf-8"), hashlib.sha256).digest()
        if hmac.compare_digest(_unb64(mac_b64), expected):
            return email
    except Exception:
        return None
    return None


# ── Store ────────────────────────────────────────────────────────────────────────

async def is_suppressed(db: AsyncSession, email: str | None,
                        tenant_id: str | None = None) -> bool:
    """True if this email is on the do-not-contact list for ``tenant_id`` (the current
    context's tenant when None): any global row, or a do-not-call row of that tenant
    (of any tenant for an all-access context)."""
    e = _norm(email)
    if not e:
        return False
    row = (
        await db.execute(select(EmailSuppressionORM.id).where(
            EmailSuppressionORM.email == e, or_(_global_row(), _scoped_row(tenant_id)),
        ).limit(1))
    ).first()
    return row is not None


async def suppress_bounced(
    db: AsyncSession,
    *,
    email: str,
    reason: str = "hard_bounce",
    source: str = "",
    recruiter_id: str | None = None,
    raw_payload: dict[str, Any] | None = None,
    event_time: datetime | None = None,
) -> bool:
    """Idempotently add an email to the do-not-contact list after a TERMINAL delivery
    failure — a hard bounce or an ESP block — so no future send (manual route,
    end-of-harvest auto-outreach, or the resend script) ever retries a dead address.
    All of those already gate on is_suppressed(), so this one write covers every path.

    Unlike add_suppression (an explicit opt-out) this deliberately does NOT flip
    RecruiterORM.unsubscribed and does NOT rewrite the outreach rows' delivery_status:
    the "bounced"/"blocked" headline set by _apply_delivery_event is the accurate
    outcome and must stand. Soft bounces (transient) and spam complaints are NOT routed
    here — see outreach_log_service._is_undeliverable. Returns True if a NEW row was
    created (False if the address was already suppressed, e.g. a prior unsubscribe)."""
    e = _norm(email)
    if not e:
        return False
    # Only a GLOBAL row makes this a no-op: a tenant's do-not-call row on the same
    # address must not stop the dead address being suppressed for everyone.
    existing = (
        await db.execute(select(EmailSuppressionORM.id).where(
            EmailSuppressionORM.email == e, _global_row()).limit(1))
    ).first()
    if existing is not None:
        return False
    db.add(EmailSuppressionORM(
        email=e,
        reason=(reason or "hard_bounce")[:30],
        source=source or "",
        recruiter_id=recruiter_id,
        raw_payload=raw_payload,
        # The provider event's own time (webhook bounce/block), so the row reflects
        # when the terminal failure occurred, not when we processed it. Defaults now().
        unsubscribed_at=event_time or datetime.now(timezone.utc),
    ))
    return True


async def add_suppression(
    db: AsyncSession,
    *,
    email: str,
    source: str,
    raw_payload: dict[str, Any] | None = None,
    recruiter_id: str | None = None,
    event_time: datetime | None = None,
) -> bool:
    """Idempotently suppress an email. Also mirrors the flag onto any recruiter whose
    official/secondary address matches, and stamps the recent outreach rows to that
    address as ``delivery_status="unsubscribed"`` (with an ``unsub`` event) so Mail
    logs reflects it. Returns True if a NEW suppression row was created.

    event_time is the provider event's own timestamp (webhook unsub) — passed so the
    recorded opt-out time reflects when the recruiter actually unsubscribed, not when
    we processed the webhook. Non-webhook callers (footer link, one-click) omit it →
    now(), the accurate click time."""
    e = _norm(email)
    if not e:
        return False
    when = event_time or datetime.now(timezone.utc)

    existing = (
        await db.execute(select(EmailSuppressionORM.id).where(
            EmailSuppressionORM.email == e, _global_row()).limit(1))
    ).first()
    created = existing is None
    if created:
        db.add(EmailSuppressionORM(
            email=e, reason="unsubscribe", source=source or "",
            recruiter_id=recruiter_id, raw_payload=raw_payload,
            unsubscribed_at=when,
        ))

    # Mirror onto matching recruiters (idempotent — sets the same values).
    await db.execute(
        update(RecruiterORM)
        .where(or_(
            func.lower(RecruiterORM.official_email_id) == e,
            func.lower(RecruiterORM.secondary_email) == e,
        ))
        .values(unsubscribed=True, unsubscribed_at=when)
    )

    # Stamp the outreach rows to this address (skip any already unsubscribed so
    # repeat calls — webhook + link — don't append duplicate events).
    rows = (
        await db.execute(select(EmailOutreachORM).where(func.lower(EmailOutreachORM.to_email) == e))
    ).scalars().all()
    for row in rows:
        if row.delivery_status == "unsubscribed":
            continue
        row.delivery_status = "unsubscribed"
        row.events = [*(row.events or []), {"event": "unsub", "at": when.isoformat()}]

    return created


async def suppress_do_not_call(
    db: AsyncSession,
    *,
    email: str,
    tenant_id: str,
    source: str = "phone_dnc",
    recruiter_id: str | None = None,
    raw_payload: dict[str, Any] | None = None,
    event_time: datetime | None = None,
) -> bool:
    """Idempotently add a TENANT-scoped do-not-call suppression (a contact asked, on a
    call logged in ``tenant_id``, not to be approached again). Like suppress_bounced it
    leaves the recruiter flag and outreach rows alone. A no-op (False) when the address
    is already suppressed globally or for this tenant; True when a row was created.
    ``raw_payload`` carries the call that asked (call_id / phone) so the row can be
    found again and lifted (phone_verification_service: outcome edit, admin unflag)."""
    e = _norm(email)
    if not e or not tenant_id:
        return False
    existing = (
        await db.execute(select(EmailSuppressionORM.id).where(
            EmailSuppressionORM.email == e,
            or_(_global_row(), and_(EmailSuppressionORM.reason == DO_NOT_CALL_REASON,
                                    EmailSuppressionORM.tenant_id == tenant_id)),
        ).limit(1))
    ).first()
    if existing is not None:
        return False
    db.add(EmailSuppressionORM(
        tenant_id=tenant_id,
        email=e,
        reason=DO_NOT_CALL_REASON,
        source=(source or "")[:30],
        recruiter_id=recruiter_id,
        raw_payload=raw_payload,
        unsubscribed_at=event_time or datetime.now(timezone.utc),
    ))
    return True


async def do_not_call_rows(db: AsyncSession, tenant_ids: Iterable[str]) -> list[EmailSuppressionORM]:
    """The do-not-call suppressions of these tenants (raw_payload says which call/number
    created each). Explicitly tenant-filtered — never another tenant's rows."""
    tids = [t for t in dict.fromkeys(tenant_ids) if t]
    if not tids:
        return []
    return list((await db.execute(select(EmailSuppressionORM).where(
        EmailSuppressionORM.reason == DO_NOT_CALL_REASON,
        EmailSuppressionORM.tenant_id.in_(tids),
    ))).scalars().all())
