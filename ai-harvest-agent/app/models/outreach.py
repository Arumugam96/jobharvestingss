"""Outreach send log — one row per recruiter-outreach email dispatched from the
Harvested Jobs UI (app/routes/outreach_routes.py::send_outreach_email).

Separate from LlmCallORM (app/models/harvest_run.py): that table records the LLM
*generation* calls (with call_type "email_generation"/"linkedin_generation");
this table records the actual *sends* — recipient, rendered content, which
audience template was used, and the delivery outcome. The two are written at
different stages (generate vs. send) so a draft can be regenerated many times
before a single send row is written.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.harvest import Base  # shared metadata — one Base.metadata.create_all() for all tables


class EmailOutreachORM(Base):
    __tablename__ = "email_outreach"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Denormalized correlation keys, not hard FKs — the source job may be a
    # JSON-sourced row with a synthetic id, and recruiters aren't always linked
    # (mirrors LlmCallORM.job_url's rationale).
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    recruiter_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Outreach channel: "email" | "linkedin". Email is sent over SMTP; LinkedIn is
    # logged when the user manually marks a copied message as sent (no programmatic
    # LinkedIn transport exists).
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="email")
    # "initial" (first contact) | "followup" (a nudge referencing an earlier send).
    outreach_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="initial")
    # Self-referential soft link to the outreach this one follows up. A thread is a
    # root row (parent NULL) plus its descendants — supports arbitrary follow-up
    # depth without a hard-coded single follow-up column.
    parent_outreach_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # The email Message-ID header captured at send time (SMTP), when available.
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # Which audience template drove the copy: "active" | "new" | "unknown"
    # (see app/services/active_clients.py::classify_client).
    client_type: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    # Generation tone the body was produced with: "Formal" | "Friendly" | "Direct".
    tone: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    to_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    from_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    attachment_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # True when the sent body came from the LLM; False when the static fallback
    # template was used (LLM generation failed). fallback_used is the inverse
    # signal captured at generation time and echoed here for the send record.
    llm_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # "sent" | "failed" — the send-time transport outcome (accepted by Mailjet or
    # not). Post-send delivery is tracked separately in delivery_status below.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="sent")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The logged-in user who triggered the send (AuthenticatedUser.email).
    sent_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # ── Delivery engagement (updated by Mailjet event webhooks) ─────────────────
    # Furthest-along delivery state seen from Mailjet's event callbacks:
    #   "sent" (accepted) → "delivered" → "opened" → "clicked", or a terminal
    #   "bounced" | "blocked" | "spam". NULL for LinkedIn rows and email rows with
    #   no events yet — the UI then falls back to the send `status` (sent/failed).
    # See app/routes/outreach_routes.py::mailjet_events. Correlated via CustomID
    # (the row id) set on the Mailjet send.
    delivery_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Reserved for the later inbound-reply-tracking phase — no writer yet.
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Full ordered trail of Mailjet events this send passed through — every event
    # (sent/open/click/spam/unsub/…) appended as {"event", "at"}, even ones that
    # don't advance the headline `delivery_status`. NULL/[] until the first event.
    # Lets the Mail logs UI show every status a mail hit, not just the latest.
    events: Mapped[list | None] = mapped_column(JSON, nullable=True)
