"""Phone-level state set by logged calls ("Verify mobile number" on the Harvested Jobs table).

  * phone_contact_flags — one row per (tenant, number). The same number shows on
    many scraped_jobs rows, so what a call learns about it (invalid, do-not-call,
    warm) lives here rather than on any single job or call log. Written by
    phone_verification_service._apply_outcome_effects; read back by the table
    through /phone-verifications/latest.

phone_key is the number's digits only (phone_verification_service.normalize_phone),
so "+91 98400 12345" and "+919840012345" share one row. Each *_at stamp is set
once, by the first call that established it. Tenant-scoped content table
(RLS + apply_tenant). Integer autoincrement PK.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.harvest import Base  # shared metadata — one create_all() for all tables


class PhoneContactFlagORM(Base):
    __tablename__ = "phone_contact_flags"
    __table_args__ = (
        Index("ux_phone_contact_flags_tenant_phone", "tenant_id", "phone_key", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default="'internal'", index=True
    )
    phone_key: Mapped[str] = mapped_column(String(50), nullable=False)  # digits only
    phone: Mapped[str] = mapped_column(String(50), nullable=False, default="")  # as dialed

    # Wrong number / invalid number (not in service) — drop from the dialler.
    invalid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Asked not to be contacted again — the contact's email is also suppressed.
    dnc_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Good response — interested.
    warm_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # "Queue for number re-enrichment" — a request only; nothing spends Apollo credits here.
    reenrich_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The number the person who answered said is correct (wrong-number calls).
    # Kept here only — the job's scraped contact is never overwritten.
    correct_number: Mapped[str] = mapped_column(String(50), nullable=False, default="")

    last_outcome: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    # phone_call_logs.id of the latest call that touched this row (soft link).
    last_call_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
