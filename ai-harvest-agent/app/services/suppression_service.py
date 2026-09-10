"""Outreach suppression (do-not-contact) — email-keyed store, guard, and tokens.

`email_suppressions` (app/models/suppression.py) is the source of truth checked
before every outreach send (see app/routes/outreach_routes.py::send_email). Keyed on
the normalized email because many sends carry only a job_id with no linked recruiter,
and Mailjet's unsub event / the unsubscribe link both identify the person by email.
When a suppressed email resolves to a recruiter, a mirror flag is set for CRM
visibility, and the recent outreach rows to that address are stamped
`delivery_status="unsubscribed"` so the Mail logs UI shows it.

Unsubscribe links carry a stateless HMAC token of the email (no per-send storage);
this module signs and verifies them.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.contact_normalize import normalize_email
from app.models.outreach import EmailOutreachORM
from app.models.recruiter import RecruiterORM
from app.models.suppression import EmailSuppressionORM


def _norm(email: str | None) -> str:
    """Normalize to a consistent lowercase key (emails are case-insensitive in
    practice; we key on lowercase everywhere)."""
    cleaned = normalize_email(email) or (email or "")
    return cleaned.strip().lower()


# ── Stateless unsubscribe token (HMAC of the email) ─────────────────────────────

def _secret() -> bytes:
    return (get_settings().jwt_secret_key or "change-me").encode("utf-8")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
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

async def is_suppressed(db: AsyncSession, email: str | None) -> bool:
    """True if this email is on the do-not-contact list."""
    e = _norm(email)
    if not e:
        return False
    row = (
        await db.execute(select(EmailSuppressionORM.id).where(EmailSuppressionORM.email == e))
    ).first()
    return row is not None


async def add_suppression(
    db: AsyncSession,
    *,
    email: str,
    source: str,
    raw_payload: dict[str, Any] | None = None,
    recruiter_id: str | None = None,
) -> bool:
    """Idempotently suppress an email. Also mirrors the flag onto any recruiter whose
    official/secondary address matches, and stamps the recent outreach rows to that
    address as ``delivery_status="unsubscribed"`` (with an ``unsub`` event) so Mail
    logs reflects it. Returns True if a NEW suppression row was created."""
    e = _norm(email)
    if not e:
        return False
    now = datetime.now(timezone.utc)

    existing = (
        await db.execute(select(EmailSuppressionORM).where(EmailSuppressionORM.email == e))
    ).scalar_one_or_none()
    created = existing is None
    if created:
        db.add(EmailSuppressionORM(
            email=e, reason="unsubscribe", source=source or "",
            recruiter_id=recruiter_id, raw_payload=raw_payload,
        ))

    # Mirror onto matching recruiters (idempotent — sets the same values).
    await db.execute(
        update(RecruiterORM)
        .where(or_(
            func.lower(RecruiterORM.official_email_id) == e,
            func.lower(RecruiterORM.secondary_email) == e,
        ))
        .values(unsubscribed=True, unsubscribed_at=now)
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
        row.events = [*(row.events or []), {"event": "unsub", "at": now.isoformat()}]

    return created
