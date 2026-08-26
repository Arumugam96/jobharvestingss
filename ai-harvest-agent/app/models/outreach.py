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

from sqlalchemy import Boolean, DateTime, String, Text, func
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
    # "sent" | "failed" — SMTP delivery outcome.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="sent")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The logged-in user who triggered the send (AuthenticatedUser.email).
    sent_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
