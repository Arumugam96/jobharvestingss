"""Outreach suppression list — the do-not-contact source of truth, keyed by email.

One row per email address that has unsubscribed from recruiter outreach. Keyed on
the *email* (normalized lowercase) rather than the recruiter, because outreach sends
often carry only a job_id with no linked recruiter, and Mailjet's unsub event / the
unsubscribe link both identify the recipient by email. The recruiter record (when
one is linked) mirrors a flag for CRM visibility, but this table is what the pre-send
guard checks. See app/services/suppression_service.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.harvest import Base  # shared metadata — one Base.metadata.create_all() for all tables


class EmailSuppressionORM(Base):
    __tablename__ = "email_suppressions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Normalized (lowercased, trimmed) recipient email — the do-not-contact key.
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    # Why the address was suppressed. Only "unsubscribe" today (spam is recorded on
    # the outreach row's event trail but does not suppress, per product decision).
    reason: Mapped[str] = mapped_column(String(30), nullable=False, default="unsubscribe")
    # How it was captured: "self_link" (footer link) | "list_unsubscribe" (one-click)
    # | "mailjet_webhook" (Mailjet unsub event) | "manual".
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    # The recruiter this email resolved to, when one was linked (mirror only).
    recruiter_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Raw webhook/request payload for auditing (Mailjet event dict, or link context).
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    unsubscribed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
