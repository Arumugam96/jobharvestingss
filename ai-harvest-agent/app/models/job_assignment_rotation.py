"""Round-robin cursor for automatic job assignment to Business Leads.

  * job_assignment_rotation — one row per tenant. ``last_assigned_email`` is the
    Business Lead who received the tenant's most recent auto-assigned job; the
    next pick is the first active Business Lead whose email sorts after it
    (wrapping round). Storing the last email rather than a counter keeps the
    rotation fair when Business Leads are added or removed.

Written only by app/services/auto_assignment_service.py, which locks the row
(SELECT ... FOR UPDATE) for the rest of the harvest-write transaction so two
concurrent writers serialise. Scoped by explicit tenant_id in SQL (like
recruiter_finder_usage) — not an RLS content table.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.harvest import Base  # shared metadata — one create_all() for all tables


class JobAssignmentRotationORM(Base):
    __tablename__ = "job_assignment_rotation"

    tenant_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    # Lower-cased email of the last Business Lead auto-assigned a job; "" = none yet.
    last_assigned_email: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
