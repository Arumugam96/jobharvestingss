"""Apollo asynchronous phone-reveal bookkeeping for the Contact Finder (/contacts).

Apollo never returns a phone in the /people/match response — it POSTs it later to the
``webhook_url`` we pass (app/services/apollo_phone_service.py receives it). These two
tables make that round-trip correlatable and observable:

  * apollo_phone_requests — one row per (phone request, person). ``ref`` is a uuid we
    embed in the webhook URL for each Apollo call; together with the Apollo person id
    in the delivery it finds the row, and the row links back to the recruiter /
    finder item / finder job / reveal-log row the phone belongs to. Tenant-scoped
    content table (listed in main._TENANT_CONTENT_TABLES).

  * apollo_webhook_events — raw log of every delivery hitting the webhook (token ok or
    not, matched or not), so "is Apollo actually delivering?". 
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.harvest import Base  # shared metadata — one Base.metadata.create_all() for all tables


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ApolloPhoneRequestORM(Base):
    """One phone request sent to Apollo for one person.

    status: pending (waiting for the webhook) | found | not_found (Apollo matched
    nobody / delivered no number) | expired (no delivery within the TTL — a late
    delivery still lands) | rejected (Apollo refused the webhook_url, the request
    went out email-only) | cancelled (we dropped the contact before persisting it).
    """
    __tablename__ = "apollo_phone_requests"
    __table_args__ = (
        Index("ix_apollo_phone_requests_ref_person", "ref", "apollo_person_id"),
        Index("ix_apollo_phone_requests_status_requested", "status", "requested_at"),
    )

    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, server_default="'internal'", index=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # One per Apollo call — embedded in the webhook URL as ?ref=…
    ref: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # Known up front for a candidate reveal; learned from the match response for a
    # LinkedIn / name lookup.
    apollo_person_id: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)

    # Where a delivered phone is written (all soft links — nullable, no hard FKs).
    recruiter_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    finder_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    finder_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    reveal_log_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(120), nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    phone: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    phone_type: Mapped[str] = mapped_column(String(30), nullable=False, default="")  # mobile | work_direct | …
    phone_status: Mapped[str] = mapped_column(String(20), nullable=False, default="")  # VERIFIED | PUBLIC
    dnc_status: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    credits_charged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ApolloWebhookEventORM(Base):
    """One raw delivery to POST /recruiter-finder/apollo-phone-webhook."""
    __tablename__ = "apollo_webhook_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now(), index=True
    )
    ref: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    remote_addr: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    token_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    people_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    found_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Raw JSON body, truncated (~64 KB) — never parsed back, diagnostics only.
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
