"""Inbound outreach replies — one row per recruiter/prospect reply captured from
the Brevo inbound-parse webhook (app/routes/outreach_routes.py::inbound-reply).

Kept SEPARATE from EmailOutreachORM (app/models/outreach.py), which is the log of
mail we SEND. This table is the log of mail we RECEIVE back, soft-linked to the
outreach it answers (``outreach_id`` — matched by the reply's In-Reply-To/References
header against the send's provider_message_id, with a sender-email fallback). The
matched send also gets its ``replied_at`` stamped so the Mail-logs "Replied" stat and
row badge can read a single denormalized flag without a join.

The unread flag (``is_read``) powers the in-app reply notification (bell + toast);
``forwarded`` records whether the per-tenant email alarm was dispatched.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.harvest import Base  # shared metadata — one Base.metadata.create_all() for all tables


class OutreachReplyORM(Base):
    __tablename__ = "outreach_replies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, server_default="'internal'", index=True)
    # The send this reply answers (EmailOutreachORM.id), when matched. NULL when the
    # reply couldn't be tied to a known send (stored anyway so nothing is lost).
    outreach_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Correlation keys copied from the matched send so the reply joins the same
    # job/recruiter thread the Mail-logs + thread page already query by.
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    recruiter_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Who replied (the prospect) and which mailbox of ours it landed on.
    from_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    from_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    to_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    company: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Plain-text reply body (quoted history trimmed when the provider supplies the
    # extracted message) + the raw HTML part when present.
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The reply's own Message-ID and the header that let us thread it to the send.
    message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    in_reply_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Notification state (bell + toast) and whether the per-tenant email alarm fired.
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false", index=True)
    forwarded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # When the prospect sent the reply (from the inbound payload), distinct from
    # created_at (our row-insert time).
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # The raw inbound payload item, kept for debugging / reprocessing.
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
