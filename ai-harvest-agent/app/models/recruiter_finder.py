"""Recruiter Contact Finder tables — the on-demand Apollo enrichment workspace
(app/services/recruiter_finder_service.py, app/routes/recruiter_finder_routes.py).

Three tables, all importing the shared Base so they register on the one
Base.metadata (one create_all for the whole app):

  * recruiter_finder_usage  — per-tenant, per-UTC-day Apollo credit counter backing
    the Contact Finder's OWN daily cap (settings.recruiter_finder_daily_cap), kept
    SEPARATE from the harvest's apollo_daily_usage. Like apollo_daily_usage it is a
    standalone counter scoped by an EXPLICIT tenant_id in SQL (NOT RLS) because the
    atomic reserve runs in its own short-lived session outside the request's RLS bind.

  * recruiter_finder_jobs   — one row per enrichment run (a single lookup or a bulk
    upload), carrying the live counters so the status endpoint is a single-row read.
    Modeled on HarvestRunORM. Tenant-scoped content table (RLS-governed).

  * recruiter_finder_items  — one row per uploaded/queued record, carrying its
    validation + live processing status and the Apollo result. Modeled on
    ReenrichmentTaskORM (the per-item status + retry pattern). Tenant-scoped.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.harvest import Base  # shared metadata — one Base.metadata.create_all() for all tables


class RecruiterFinderUsageORM(Base):
    """Per-tenant, per-UTC-day Apollo credit counter for the Contact Finder.

    One row per (usage_date, tenant_id); `count` is bumped atomically before each
    credit-spending Apollo reveal and refused once it would exceed the tenant's cap
    (app/services/recruiter_finder_budget.py). Standalone counter — scoped by
    explicit tenant_id in the SQL, NOT by RLS (so it stays OUT of _TENANT_CONTENT_TABLES,
    mirroring apollo_daily_usage).
    """
    __tablename__ = "recruiter_finder_usage"

    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True, server_default="'internal'")
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RecruiterFinderJobORM(Base):
    """One Contact Finder run (single lookup or bulk upload) + its live counters.

    `message` carries the human "currently processing <name>…" text (exactly like
    HarvestRunService.update_run drives the harvest status line), and the counters
    let GET /recruiter-finder/jobs/{id} be a single indexed row read.
    """
    __tablename__ = "recruiter_finder_jobs"

    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, server_default="'internal'", index=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    requested_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="upload")  # "upload" | "single"
    filename: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    persona: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    # Ordered, newline-joined "Who to look up" roles for a bulk upload (the multi-select).
    # The worker walks them top-to-bottom per company and reveals the first that yields a
    # contact. `persona` holds the first selection for backward-compatible display.
    personas: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    reveal: Mapped[str] = mapped_column(String(10), nullable=False, default="email")  # "email" | "phone" | "both"
    # queued | running | completed | partial | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", index=True)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")

    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    not_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ambiguous: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queued: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    credits_spent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RecruiterFinderItemORM(Base):
    """One uploaded/queued record: its validation verdict, live processing status,
    and the Apollo result. The composite indexes serve the two hot queries — list a
    job's rows by status, and "fetch next N pending oldest-first" for the worker.
    """
    __tablename__ = "recruiter_finder_items"
    __table_args__ = (
        Index("ix_recruiter_finder_items_job_status", "job_id", "status"),
        Index("ix_recruiter_finder_items_status_seen", "status", "first_seen_at"),
    )

    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, server_default="'internal'", index=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str] = mapped_column(ForeignKey("recruiter_finder_jobs.id"), nullable=False, index=True)
    row_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── Input (already trimmed/sanitized; company is mandatory for a valid row) ──
    company: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    person_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    linkedin_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    domain: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    location: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    # valid | missing_company | invalid | duplicate
    validation_status: Mapped[str] = mapped_column(String(20), nullable=False, default="valid")
    # pending | processing | completed | not_found | ambiguous | failed | queued
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── Apollo result ──
    contact_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    contact_title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(320), nullable=False, default="")
    email_status: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    phone: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    confidence: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    # The recruiters.id the contact was upserted into (nullable, not a hard FK so a
    # purge of recruiters can't block item rows).
    recruiter_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RecruiterFinderRevealLogORM(Base):
    """One row per Apollo contact ACTUALLY revealed from the Single-search section
    (company browse or specific-person), powering the History tab. Separate from
    `recruiters` (the deduped contact store) so it is an append-only audit trail —
    who revealed what, when, which reveal type, and the credit it cost — and keeps the
    richer Apollo fields (secondary email, org industry/size, precise location) that the
    deduped recruiter record doesn't carry. Tenant-scoped (RLS + apply_tenant)."""
    __tablename__ = "recruiter_finder_reveal_log"
    __table_args__ = (
        Index("ix_rf_reveal_log_tenant_created", "tenant_id", "created_at"),
    )

    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, server_default="'internal'", index=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    person_id: Mapped[str] = mapped_column(String(60), nullable=False, default="")  # Apollo person id

    contact_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    contact_title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    company: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    company_domain: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    email: Mapped[str] = mapped_column(String(320), nullable=False, default="")
    email_status: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    secondary_email: Mapped[str] = mapped_column(String(320), nullable=False, default="")
    phone: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    phone_status: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    linkedin_url: Mapped[str] = mapped_column(Text, nullable=False, default="")

    city: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    state: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    country: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    industry: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    company_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    confidence: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    reveal_type: Mapped[str] = mapped_column(String(10), nullable=False, default="email")  # email | phone | both
    credits_spent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="")  # company_browse | person | company_single
    requested_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    recruiter_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
