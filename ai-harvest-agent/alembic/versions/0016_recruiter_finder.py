"""Recruiter Contact Finder: per-tenant Apollo usage counter, enrichment jobs +
per-row items, and recruiters provenance columns.

Adds, idempotently (safe whether the app's startup create_all / _ensure_* helpers
already ran — create_all never alters an existing table):
  * recruiter_finder_usage  — per-tenant, per-UTC-day Apollo credit counter
    (app/services/recruiter_finder_budget.py). Standalone counter, NO RLS (scoped by
    explicit tenant_id in SQL, like apollo_daily_usage).
  * recruiter_finder_jobs   — one enrichment run + its live counters (tenant-scoped).
  * recruiter_finder_items  — one uploaded/queued record + status + Apollo result.
  * recruiters.source_label / requested_by — Contact Finder provenance.

Tenant RLS is installed here for the two tenant-scoped tables (mirrors 0013), and is
also re-asserted at runtime by app/main.py::_ensure_rls_policies. Portable DDL so this
works on PostgreSQL and SQLite alike.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-07

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_INTERNAL = sa.text("'internal'")

_RLS_TABLES = ["recruiter_finder_jobs", "recruiter_finder_items"]
_RLS_PREDICATE = (
    "current_setting('app.tenant_id', true) IS NULL "
    "OR current_setting('app.tenant_id', true) = '' "
    "OR current_setting('app.tenant_id', true) = '__all__' "
    "OR tenant_id = current_setting('app.tenant_id', true)"
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    # ── recruiters provenance columns ──
    if "recruiters" in tables:
        existing = {c["name"] for c in inspector.get_columns("recruiters")}
        if "source_label" not in existing:
            bind.execute(sa_text(
                "ALTER TABLE recruiters ADD COLUMN source_label VARCHAR(60) NOT NULL DEFAULT ''"
            ))
        if "requested_by" not in existing:
            bind.execute(sa_text("ALTER TABLE recruiters ADD COLUMN requested_by VARCHAR(120)"))

    # ── recruiter_finder_usage (per-tenant daily counter; no RLS) ──
    if "recruiter_finder_usage" not in tables:
        op.create_table(
            "recruiter_finder_usage",
            sa.Column("usage_date", sa.Date(), primary_key=True),
            sa.Column("tenant_id", sa.String(length=40), primary_key=True, server_default=_INTERNAL),
            sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    # ── recruiter_finder_jobs ──
    if "recruiter_finder_jobs" not in tables:
        op.create_table(
            "recruiter_finder_jobs",
            sa.Column("tenant_id", sa.String(length=40), nullable=False, server_default=_INTERNAL),
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("requested_by", sa.String(length=120), nullable=True),
            sa.Column("source", sa.String(length=20), nullable=False, server_default="upload"),
            sa.Column("filename", sa.String(length=300), nullable=False, server_default=""),
            sa.Column("persona", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("reveal", sa.String(length=10), nullable=False, server_default="email"),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
            sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("message", sa.Text(), nullable=False, server_default=""),
            sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("processed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("not_found", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("ambiguous", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("queued", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("credits_spent", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_recruiter_finder_jobs_tenant_id", "recruiter_finder_jobs", ["tenant_id"])
        op.create_index("ix_recruiter_finder_jobs_status", "recruiter_finder_jobs", ["status"])

    # ── recruiter_finder_items ──
    if "recruiter_finder_items" not in tables:
        op.create_table(
            "recruiter_finder_items",
            sa.Column("tenant_id", sa.String(length=40), nullable=False, server_default=_INTERNAL),
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("job_id", sa.String(length=36), sa.ForeignKey("recruiter_finder_jobs.id"), nullable=False),
            sa.Column("row_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("company", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("person_name", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("linkedin_url", sa.Text(), nullable=False, server_default=""),
            sa.Column("domain", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("location", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("title", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("validation_status", sa.String(length=20), nullable=False, server_default="valid"),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("contact_name", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("contact_title", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("email", sa.String(length=320), nullable=False, server_default=""),
            sa.Column("email_status", sa.String(length=30), nullable=False, server_default=""),
            sa.Column("phone", sa.String(length=50), nullable=False, server_default=""),
            sa.Column("confidence", sa.String(length=20), nullable=False, server_default=""),
            sa.Column("recruiter_id", sa.String(length=36), nullable=True),
            sa.Column("error", sa.Text(), nullable=False, server_default=""),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_recruiter_finder_items_tenant_id", "recruiter_finder_items", ["tenant_id"])
        op.create_index("ix_recruiter_finder_items_job_id", "recruiter_finder_items", ["job_id"])
        op.create_index("ix_recruiter_finder_items_status", "recruiter_finder_items", ["status"])
        op.create_index("ix_recruiter_finder_items_job_status", "recruiter_finder_items", ["job_id", "status"])
        op.create_index("ix_recruiter_finder_items_status_seen", "recruiter_finder_items", ["status", "first_seen_at"])

    # ── RLS for the two tenant-scoped tables (PostgreSQL only; mirrors 0013) ──
    if bind.dialect.name == "postgresql":
        for table in _RLS_TABLES:
            bind.execute(sa_text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            bind.execute(sa_text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
            bind.execute(sa_text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
            bind.execute(sa_text(
                f"CREATE POLICY tenant_isolation ON {table} "
                f"USING ({_RLS_PREDICATE}) WITH CHECK ({_RLS_PREDICATE})"
            ))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    if bind.dialect.name == "postgresql":
        for table in _RLS_TABLES:
            if table in tables:
                bind.execute(sa_text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))

    if "recruiter_finder_items" in tables:
        op.drop_table("recruiter_finder_items")
    if "recruiter_finder_jobs" in tables:
        op.drop_table("recruiter_finder_jobs")
    if "recruiter_finder_usage" in tables:
        op.drop_table("recruiter_finder_usage")

    if "recruiters" in tables:
        existing = {c["name"] for c in inspector.get_columns("recruiters")}
        if "requested_by" in existing:
            op.drop_column("recruiters", "requested_by")
        if "source_label" in existing:
            op.drop_column("recruiters", "source_label")
