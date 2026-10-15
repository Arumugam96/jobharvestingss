"""Naukri Resdex candidate matching (app/services/resdex_match_service.py).

For each job that goes to automatic outreach with a recruiter/job-posting email,
the JD is turned into Resdex requirements + a Boolean query, Resdex is searched
with the logged-in recruiter session, and the 2-3 best candidates are kept:

  resdex_searches     one row per (job, search pass): requirements, query, filters,
                      outcome status
  resdex_candidates   the picked candidates of a search, as shown on Resdex
  resdex_daily_usage  account-wide per-UTC-day search / candidate counters (the
                      hard daily cap — app/services/resdex_budget.py)

Searches link to their job by (tenant_id, job_url) + run_id — stable across the
end-of-run replace_run_jobs delete+reinsert, unlike scraped_jobs.id (same as
ReenrichmentTaskORM). Candidate fields are stored only as Resdex shows them —
never inferred.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.harvest import Base


class ResdexSearchStatus:
    MATCHED = "matched"              # ≥1 candidate kept
    NO_RESULTS = "no_results"        # searched (all tiers), nothing good enough
    SESSION_EXPIRED = "session_expired"
    SESSION_BUSY = "session_busy"    # recruiter login window held the profile
    CAP_REACHED = "cap_reached"      # daily search / candidate cap
    ERROR = "error"
    SKIPPED_NO_JD = "skipped_no_jd"


class ResdexSearchORM(Base):
    __tablename__ = "resdex_searches"
    __table_args__ = (Index("ix_resdex_searches_job", "tenant_id", "job_url", "created_at"),)

    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, server_default="'internal'", index=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    job_url: Mapped[str] = mapped_column(Text, nullable=False)   # normalized (outreach_log_service)
    job_title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    company: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    # LLM (or fallback) requirement extraction, grounded in the JD.
    requirements: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    boolean_query: Mapped[str] = mapped_column(Text, nullable=False, default="")
    filters: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tier: Mapped[int] = mapped_column(Integer, nullable=False, default=0)      # ladder tier that produced the result
    searches_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default=ResdexSearchStatus.ERROR)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    candidates: Mapped[list["ResdexCandidateORM"]] = relationship(
        back_populates="search", lazy="selectin", order_by="ResdexCandidateORM.rank",
        cascade="all, delete-orphan",
    )


class ResdexCandidateORM(Base):
    __tablename__ = "resdex_candidates"

    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, server_default="'internal'", index=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    search_id: Mapped[str] = mapped_column(ForeignKey("resdex_searches.id", ondelete="CASCADE"), nullable=False, index=True)
    job_url: Mapped[str] = mapped_column(Text, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    headline: Mapped[str] = mapped_column(Text, nullable=False, default="")
    current_designation: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    current_company: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    previous_roles: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    experience_text: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    experience_years: Mapped[float | None] = mapped_column(Float, nullable=True)
    location: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    preferred_locations: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    skills: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    education: Mapped[str] = mapped_column(Text, nullable=False, default="")
    notice_period: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    ctc: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    last_active: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    profile_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    matched_skills: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    skill_coverage: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    extraction_method: Mapped[str] = mapped_column(String(10), nullable=False, default="")  # json | llm
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # email_outreach.id of the outreach email this candidate was included in.
    outreach_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    search: Mapped[ResdexSearchORM] = relationship(back_populates="candidates")


class ResdexUsageORM(Base):
    """Account-wide per-UTC-day Resdex counters — one recruiter account, so global
    (not tenant-scoped), like apollo_daily_usage."""
    __tablename__ = "resdex_daily_usage"

    usage_date: Mapped[datetime] = mapped_column(Date, primary_key=True)
    searches: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    candidates: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
