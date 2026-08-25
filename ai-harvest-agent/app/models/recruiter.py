"""Recruiter identity table — one row per unique person, linked to every
job they've posted via ScrapedJobORM.recruiter_id (app/models/harvest_run.py).

Replaces the in-memory dedup dicts previously rebuilt from JSON on every
read (recruiter_contact_agent.py, lead_merge_agent.py, orchestrator_agent.py)
with a single DB-enforced identity, resolved via app/services/recruiter_service.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.harvest import Base  # shared metadata — one Base.metadata.create_all() for all tables


class RecruiterORM(Base):
    __tablename__ = "recruiters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # linkedin_profile_url when known, else "nc:{slug(name)}|{slug(company)}" —
    # see app/services/recruiter_service.py::compute_dedup_key. Enforces
    # one identity per person at the DB level instead of per-caller dict merges.
    dedup_key: Mapped[str] = mapped_column(String(600), nullable=False, unique=True, index=True)
    # Always populated (unlike dedup_key, which switches to the LinkedIn-URL
    # form once one is known) — the fallback lookup key so a recruiter first
    # seen *with* a URL can still be found by a later URL-less job posting.
    # Not unique: two different real people can collide on name+company;
    # merging them is the same heuristic tradeoff the old JSON-scan dedup made.
    name_company_key: Mapped[str] = mapped_column(String(600), nullable=False, index=True)
    linkedin_profile_url: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    person_name: Mapped[str] = mapped_column(String(255), nullable=False)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # LinkedIn headline / role free text — no real length ceiling (scraped
    # values exceed 255 chars), so Text not String(255). Same data class as
    # ScrapedJobORM.job_poster_designation and linkedin_headline below.
    designation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    harvest_source: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # ── Contact-discovery cache (app/services/recruiter_service.py::save_enrichment) ──
    # Written back by RecruiterContactAgent/ProspectIntelligenceAgent after
    # scraped enrichment, mirroring app/models/prospect_models.py::ProspectResult
    # so a person enriched once by either pipeline doesn't get re-scraped by
    # the other. NOT_FOUND/"" defaults match ProspectResult's own defaults.
    company_domain: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    company_website: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    official_email_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    email_status: Mapped[str] = mapped_column(String(20), nullable=False, default="NOT_FOUND")
    contact_number: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    phone_status: Mapped[str] = mapped_column(String(20), nullable=False, default="NOT_FOUND")
    linkedin_headline: Mapped[str] = mapped_column(Text, nullable=False, default="")
    location: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    reporting_hierarchy: Mapped[str] = mapped_column(Text, nullable=False, default="")
    position_level: Mapped[str] = mapped_column(String(50), nullable=False, default="NOT_FOUND")
    employment_type: Mapped[str] = mapped_column(String(50), nullable=False, default="NOT_FOUND")
    years_in_company: Mapped[str] = mapped_column(String(100), nullable=False, default="NOT_FOUND")
    overall_experience: Mapped[str] = mapped_column(String(100), nullable=False, default="NOT_FOUND")
    hiring_domain: Mapped[str] = mapped_column(String(100), nullable=False, default="NOT_FOUND")
    company_industry: Mapped[str] = mapped_column(String(255), nullable=False, default="NOT_FOUND")
    company_size: Mapped[str] = mapped_column(String(100), nullable=False, default="NOT_FOUND")
    department: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    confidence_score: Mapped[str] = mapped_column(String(10), nullable=False, default="Low")
    last_enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set only when contact info was actually confirmed present during an
    # enrichment pass (not merely attempted) — see
    # app/services/recruiter_service.py::save_enrichment's `verified` kwarg.
    last_verified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Provenance of the last contact hit: "" (scraped) | "apollo" | future sources.
    # Lets us distinguish Apollo-sourced contacts and audit credit spend.
    enrichment_source: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    # Timestamp of the last Apollo lookup *attempt* (hit or miss) — the recheck
    # cooldown (settings.apollo_recheck_days) reads this to avoid re-billing the
    # same profile every run. Stays null until Apollo is first tried.
    apollo_enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # CRM/lead-pipeline status (e.g. "New", "Contacted", "Qualified") — set by
    # whatever downstream CRM sync exists; this table doesn't manage its
    # lifecycle, just carries it alongside the recruiter's identity.
    crm_status: Mapped[str | None] = mapped_column(String(50), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    jobs: Mapped[list["ScrapedJobORM"]] = relationship(back_populates="recruiter", lazy="selectin")


class RecruiterDiscoveryRunORM(Base):
    """One row per RecruiterContactAgent.run() call — summary + output file
    paths only (mirrors HarvestRunORM's design: it doesn't duplicate
    ScrapedJobORM's per-job detail either). The per-recruiter results with
    full enrichment_audit stay in the JSON file at json_path; this table
    exists so GET /recruiter-results can list/locate past runs from the DB
    instead of glob-scanning data/results/lead_intelligence/ on every call.
    """
    __tablename__ = "recruiter_discovery_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(String(40), nullable=False, unique=True, index=True)
    source_filter: Mapped[str] = mapped_column(String(20), nullable=False, default="all")
    harvest_sources: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    total_recruiters: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enriched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    high_confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    medium_confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    low_confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verified_emails: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    public_emails: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verified_phones: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    public_phones: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    no_contact: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    runtime_minutes: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    json_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    excel_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    debug_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
