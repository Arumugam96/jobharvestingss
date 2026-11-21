"""Company classification overrides + editable classification keywords.

  * company_overrides — one row per (tenant, company_key): a company-level
    hiring_entity / company_size / domain that future harvests apply to that
    company's jobs (BusinessFilterService consults it before any keyword rule;
    HarvestRunService.bulk_insert_scraped_jobs applies it at insert time).
    company_key is app.core.job_url.normalize_company_key(company name).
    source "manual" rows come from an admin (Company classification page) or a
    job edit with "apply to company"; source "llm" rows cache the harvest's LLM
    verdict for a company the keyword rules left Ambiguous. A manual write always
    replaces an llm row; an llm write never touches a manual row. An llm row with
    every field NULL means "asked, LLM didn't know" — it stops the company being
    asked again without overriding anything.
  * classification_keywords — tenant-added terms merged into the JSON master
    lists (data/master/*.json) for the staffing / direct / gcc keyword rules. Read
    per harvest run, so additions apply without a restart.

Tenant-scoped content tables (RLS + explicit tenant_id filters). Integer
autoincrement PKs. Migration: alembic 0041_company_overrides.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.harvest import Base  # shared metadata — one create_all() for all tables

OVERRIDE_SOURCE_MANUAL = "manual"
OVERRIDE_SOURCE_LLM = "llm"
OVERRIDE_SOURCES = (OVERRIDE_SOURCE_MANUAL, OVERRIDE_SOURCE_LLM)

KEYWORD_LISTS = ("staffing", "direct", "gcc")


class CompanyOverrideORM(Base):
    __tablename__ = "company_overrides"
    __table_args__ = (
        Index("ux_company_overrides_tenant_key", "tenant_id", "company_key", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default="'internal'", index=True
    )
    company_key: Mapped[str] = mapped_column(String(500), nullable=False)   # normalize_company_key
    company_name: Mapped[str] = mapped_column(String(500), nullable=False, default="")  # display
    # NULL = no override for that field (the normal rules apply).
    hiring_entity: Mapped[str | None] = mapped_column(String(50), nullable=True)
    company_size: Mapped[str | None] = mapped_column(String(100), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default=OVERRIDE_SOURCE_MANUAL)
    updated_by_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ClassificationKeywordORM(Base):
    __tablename__ = "classification_keywords"
    __table_args__ = (
        Index("ux_classification_keywords_tenant_list_term", "tenant_id", "list", "term", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default="'internal'", index=True
    )
    # Column "list"; attribute renamed so it doesn't shadow the builtin.
    list_name: Mapped[str] = mapped_column("list", String(20), nullable=False)  # staffing | direct | gcc
    term: Mapped[str] = mapped_column(String(200), nullable=False)   # lower-cased, whitespace collapsed
    created_by_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
