"""Harvest workflow ORM models: run tracking, scraped jobs, and LLM call audit log.

Covers the LinkedIn/Naukri/Dice job-board pipeline (both the multi-source
orchestrator flow and the three standalone single-source flows) — distinct
from the unrelated generic web-harvester tables in app/models/harvest.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.harvest import Base  # shared metadata — one Base.metadata.create_all() for all tables


# ── ORM Models ───────────────────────────────────────────────────────────────────

class HarvestRunORM(Base):
    __tablename__ = "harvest_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    run_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    # NULL = multi-source orchestrator run; "LinkedIn" | "Naukri" | "Dice" = single-source run
    source: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    sources: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    filters_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    linkedin_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    naukri_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dice_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    combined_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verified_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    direct_clients: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gcc: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    staffing_firms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ambiguous: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    excel_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    json_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # A run that ended via user-requested stop persists its partial jobs but does
    # NOT send its report email. report_pending=True marks it as owed; the next
    # successful run merges these jobs into its own report, then clears the flag.
    report_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    jobs: Mapped[list["ScrapedJobORM"]] = relationship(back_populates="run", lazy="selectin")


class ScrapedJobORM(Base):
    __tablename__ = "scraped_jobs"
    __table_args__ = (Index("ix_scraped_jobs_run_source", "run_id", "source"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("harvest_runs.id"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    job_title: Mapped[str] = mapped_column(String(500), nullable=False)
    company: Mapped[str] = mapped_column(String(500), nullable=False)
    location: Mapped[str] = mapped_column(String(500), nullable=False)
    # Scraped free text with no reliable length ceiling — Text, not String(255).
    # Job boards occasionally emit long descriptive salary/experience strings
    # that overflow 255 in larger datasets (they were short in the small local
    # sample but not on the full EC2 data).
    salary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    experience: Mapped[str] = mapped_column(Text, nullable=False, default="")
    posted_date: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    job_url: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    job_description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Sanitized inner HTML of the LinkedIn description container, kept separate
    # from the plain-text job_description so the JSON/Excel reports (which read
    # job_description via scraped_job_view) stay tag-free. Empty for Naukri/Dice
    # and any job where the description container couldn't be captured — the UI
    # falls back to rendering the plain-text job_description in that case.
    job_description_html: Mapped[str] = mapped_column(Text, nullable=False, default="")
    skills: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    work_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="not_specified")
    # Present on LinkedIn/Dice's own scraped dataclasses (and their *Job response
    # models) but not on UnifiedJob — kept here so /linkedin-results and
    # /dice-results don't lose data relative to the file-based responses.
    company_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    # LinkedIn employee-range band captured per job (e.g. "1,001-5,000 employees"),
    # LLM-extracted with a regex fallback over the scraped job-insights text. Empty
    # ("Unknown") for jobs where the band wasn't shown and for non-LinkedIn sources.
    # Used only to power a display-time company-size filter in the UI — never to
    # drop jobs at harvest time. See app/core/company_size.py.
    company_size: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    employment_type: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    job_type: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    domain: Mapped[str] = mapped_column(String(50), nullable=False, default="Any")
    hiring_entity: Mapped[str] = mapped_column(String(50), nullable=False, default="Any")
    is_gcc: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verification_status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    # LLM-extraction state for the re-enrichment workflow:
    #   "ok"      — extracted normally (selector or LLM) — the default for every row
    #   "pending" — every LLM provider was down at scrape time; this row holds
    #               selector-only data and a reenrichment_tasks row is queued
    #   "failed"  — re-enrichment never succeeded within the retention window
    # Set by targeted UPDATE (not through UnifiedJob.to_dict), so it survives the
    # end-of-run replace_run_jobs delete+reinsert.
    extraction_status: Mapped[str] = mapped_column(String(20), nullable=False, default="ok")
    # BusinessFilterService annotation — the filter no longer removes jobs, it
    # flags them. passed_filter=False means the job failed a filter rule but is
    # still retained/shown; filter_reason records the first failing stage+value.
    passed_filter: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    filter_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    job_poster_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    # LinkedIn "poster headline" free text — no real length ceiling (scraped
    # values exceed 255 chars), so Text not String(255). Mirrors
    # RecruiterORM.designation / linkedin_headline.
    job_poster_designation: Mapped[str | None] = mapped_column(Text, nullable=True)
    linkedin_profile_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_company: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Canonical recruiter identity (app/models/recruiter.py), resolved at
    # insert time by app/services/recruiter_service.py::upsert_recruiter.
    # job_poster_name/linkedin_profile_url above stay as-is — the raw
    # snapshot scraped for *this* job — this FK is the merged identity link
    # that lets one recruiter's many job postings be queried together.
    recruiter_id: Mapped[str | None] = mapped_column(ForeignKey("recruiters.id"), nullable=True, index=True)
    # LLM lead-quality score (0.0–1.0) for the LinkedIn Home Feed workflow — a
    # genuine IT-hiring post classified + extracted from the feed carries the
    # confidence the classifier assigned (blended with independent signal count).
    # Nullable: rows from every other source/flow (LinkedIn Jobs/Naukri/Dice) leave
    # it NULL, so it never affects their reads.
    lead_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    run: Mapped[HarvestRunORM] = relationship(back_populates="jobs")
    # lazy="selectin": the recruiter must be loaded eagerly — read paths merge
    # RecruiterORM.official_email_id/contact_number into the job's own
    # email/phone (see harvest_run_service.scraped_job_view), and a lazy load
    # would raise MissingGreenlet under the async session.
    recruiter: Mapped["RecruiterORM | None"] = relationship(back_populates="jobs", lazy="selectin")


class LlmCallType:
    """Purpose discriminator for LlmCallORM.call_type — lets one audit table
    cover every LLM call across the app, tagged by which workflow it belongs to
    so rows are easy to classify:

      job_harvest         — extracting scraped-job data (title/company/JD/etc.)
      contact_harvest     — extracting recruiter email/phone contact details
      feed_classify       — classifying a LinkedIn Home Feed post as an IT hiring
                            lead (stage 1 of the feed workflow — cheap, per candidate)
      feed_extract        — extracting job + recruiter data from a feed post that
                            classified as a genuine IT hiring lead (stage 2)
      email_generation    — composing an outreach email (outreach flow)
      email_followup      — composing a follow-up outreach email (outreach flow)
      linkedin_generation — composing a LinkedIn message (outreach flow)

    Harvest calls come from the scrape pipeline (run-scoped); the generation
    types come from the outreach flow (run_id is NULL). NOTE: rows written before
    this rename retain the legacy value "harvest" (and any stray
    "contact_extraction")."""
    JOB_HARVEST = "job_harvest"
    CONTACT_HARVEST = "contact_harvest"
    FEED_CLASSIFY = "feed_classify"
    FEED_EXTRACT = "feed_extract"
    EMAIL_GENERATION = "email_generation"
    EMAIL_FOLLOWUP = "email_followup"
    LINKEDIN_GENERATION = "linkedin_generation"


class LlmCallORM(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_run_called_at", "run_id", "called_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Nullable: harvest/extraction calls belong to a run; ad-hoc outreach
    # generations (email/linkedin from the UI) have no harvest run, so they are
    # stored with run_id=NULL. FK constraint kept, only NOT NULL dropped.
    run_id: Mapped[str | None] = mapped_column(ForeignKey("harvest_runs.id"), nullable=True, index=True)
    # What this LLM call was for — see LlmCallType. Defaults to "job_harvest";
    # pre-rename rows retain the legacy "harvest" DB server-default value.
    call_type: Mapped[str] = mapped_column(String(30), nullable=False, default=LlmCallType.JOB_HARVEST)
    # Denormalized correlation key, not a hard FK — the LLM fallback fires mid-scrape,
    # before ScrapedJobORM rows exist for the run's final deduped job list.
    job_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    response_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    called_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReenrichmentTaskORM(Base):
    """A job whose LLM extraction degraded to selector-only because every LLM
    provider (local + configured fallback) was down at scrape time. Stores the
    exact replayable extraction payload (content/schema/system) so a later
    harvest run's start-of-run sweep can re-run extraction once the LLM is back
    and backfill the scraped_jobs row — without re-scraping the (possibly
    expired) LinkedIn page. Retried until it succeeds or ages past the retention
    window (settings.reenrichment_max_age_days), after which it is marked failed.

    Linked to its scraped_jobs row by (run_id, job_url) — stable across the
    end-of-run replace_run_jobs delete+reinsert, unlike the row's UUID.
    """
    __tablename__ = "reenrichment_tasks"
    __table_args__ = (Index("ix_reenrichment_tasks_status", "status", "first_seen_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(ForeignKey("harvest_runs.id"), nullable=False, index=True)
    job_url: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    # Replay payload — passed verbatim to LLMService.extract_json on retry.
    content: Mapped[str] = mapped_column(Text, nullable=False)
    schema_description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    system: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # "pending" — awaiting a successful re-extraction
    # "done"    — re-enriched; the scraped_jobs row was backfilled
    # "failed"  — aged past the retention window without success
    # (No index=True here — the composite ix_reenrichment_tasks_status below leads
    # with `status`, so it already serves status lookups; a second index would
    # collide on the auto-generated name.)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
