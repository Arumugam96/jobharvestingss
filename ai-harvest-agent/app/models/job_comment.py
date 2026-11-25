"""Free-text comments on a job (My Workspace "Comments" column).

  * job_comments — a timestamped history of notes per scraped job, written by the
    job's assignee (or an admin). Unlike a call's remarks (phone_call_logs.remarks),
    a comment needs no phone number, so email-only jobs (common for LinkedIn Feed
    leads) can carry notes too. The workspace shows the newest of the latest
    call's remarks and the latest comment, falling back to the job's
    assignment_note when neither exists.

job_id is a soft link to scraped_jobs.id (no FK — run reconcile re-inserts job
rows, same as phone_call_logs). Tenant-scoped content table (RLS + apply_tenant).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.harvest import Base  # shared metadata — one create_all() for all tables


class JobCommentORM(Base):
    __tablename__ = "job_comments"
    __table_args__ = (
        Index("ix_job_comments_job_created", "job_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default="'internal'", index=True
    )
    # scraped_jobs.id — soft link (no FK; run reconcile reinserts job rows).
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    author_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    author_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
