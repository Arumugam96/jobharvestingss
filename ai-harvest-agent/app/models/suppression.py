"""Outreach suppression list — the do-not-contact source of truth, keyed by email.

One row per email address that has unsubscribed from recruiter outreach. Keyed on
the *email* (normalized lowercase) rather than the recruiter, because outreach sends
often carry only a job_id with no linked recruiter, and Mailjet's unsub event / the
unsubscribe link both identify the recipient by email. The recruiter record (when
one is linked) mirrors a flag for CRM visibility, but this table is what the pre-send
guard checks. See app/services/suppression_service.py.

Scope (suppression_service.is_suppressed): unsubscribes and terminal bounces/blocks
are GLOBAL — a property of the address, honoured for every tenant. A "do_not_call"
row (a contact's request on a logged call) is TENANT-scoped: it stops only the tenant
whose caller logged it (all-access/internal context still honours every tenant's).
Uniqueness is therefore (tenant_id, email, reason), not email alone.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.harvest import Base  # shared metadata — one Base.metadata.create_all() for all tables


class EmailSuppressionORM(Base):
    __tablename__ = "email_suppressions"
    __table_args__ = (
        # One row per tenant + address + reason (see the module docstring). Mirrored by
        # app/main.py::_ensure_email_suppressions_scope and alembic 0042.
        Index("ux_email_suppressions_tenant_email_reason", "tenant_id", "email", "reason",
              unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Owning tenant (for RLS/visibility). For a "do_not_call" row it is also the
    # scope of the suppression; unsubscribe / bounce rows apply to every tenant.
    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, server_default="'internal'", index=True)
    # Normalized (lowercased, trimmed) recipient email — the do-not-contact key.
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # Why the address was suppressed: "unsubscribe" (opt-out), or a TERMINAL delivery
    # failure — "hard_bounce" / "blocked" — added by suppression_service.suppress_bounced
    # so a dead address is never retried. (Soft bounces and spam are recorded on the
    # outreach row's event trail but do NOT suppress, per product decision.)
    # "do_not_call" — the contact asked on a logged call not to be approached again
    # (phone_verification_service, source "phone_dnc").
    reason: Mapped[str] = mapped_column(String(30), nullable=False, default="unsubscribe")
    # How it was captured: "self_link" (footer link) | "list_unsubscribe" (one-click)
    # | "mailjet_webhook" / "brevo_webhook" (provider unsub or hard-bounce/block event)
    # | "manual" | "phone_dnc" (do-not-call outcome on a logged call).
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    # The recruiter this email resolved to, when one was linked (mirror only).
    recruiter_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Raw webhook/request payload for auditing (Mailjet event dict, or link context).
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    unsubscribed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
