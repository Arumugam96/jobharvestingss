"""Phone call verification log ("Verify mobile number" on the Harvested Jobs table).

  * phone_call_logs — one append-only row per call a user logged against a job's
    phone number (app/services/phone_verification_service.py). The newest
    non-cleared row per job drives the table badge; the history is the full trail.
    An Unanswered call with Follow-up on carries follow_up_due_at, which surfaces in
    the caller's notification bell once it passes (computed at poll time).

Job, company, title, run and recruiter are NOT duplicated here: they are reached
through job_id → scraped_jobs. job_id is a soft link (no FK) because a run
reconcile deletes and reinserts scraped_jobs rows; relink_job_ids() re-points it.
Tenant-scoped content table (RLS + apply_tenant). Integer autoincrement PK.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.harvest import Base  # shared metadata — one create_all() for all tables


class PhoneCallLogORM(Base):
    __tablename__ = "phone_call_logs"
    __table_args__ = (
        Index("ix_phone_call_logs_tenant_job", "tenant_id", "job_id"),
        Index("ix_phone_call_logs_caller_due", "called_by_email", "follow_up_due_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default="'internal'", index=True
    )

    # scraped_jobs.id — soft link (no FK; run reconcile reinserts job rows).
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    phone: Mapped[str] = mapped_column(String(50), nullable=False, default="")  # as dialed
    # job | recruiter | contact | company (the UI's phoneParts[0].variant)
    phone_source: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    # Whose number it was at call time (the job's contact can be re-enriched later).
    contact_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    call_status: Mapped[str] = mapped_column(String(20), nullable=False)  # answered | unanswered
    remarks: Mapped[str] = mapped_column(Text, nullable=False, default="")
    follow_up: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Set only for unanswered + follow_up: created_at + PHONE_FOLLOWUP_DELAY_HOURS.
    follow_up_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Dismissed by the caller, or superseded by a newer call on the same job+phone.
    follow_up_done_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    called_by_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    called_by_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    # "Clear status" soft-delete.
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
