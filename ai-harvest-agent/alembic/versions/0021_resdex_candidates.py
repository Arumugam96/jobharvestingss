"""Resdex candidate matching: searches, candidates, daily usage.

Adds resdex_searches (one row per job search pass: LLM requirements, Boolean query,
filters, status), resdex_candidates (the 2-3 picked candidates as shown on Resdex)
and resdex_daily_usage (account-wide per-UTC-day search / candidate counters).
The two content tables are tenant-scoped (RLS mirrors 0016/0017); the usage
counter is global like apollo_daily_usage. Mirrors app/models/resdex.py — the
running app creates these via create_all at startup.

Idempotent (safe alongside the app's startup create_all). Portable DDL (PostgreSQL + SQLite).

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-14

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

_INTERNAL = sa.text("'internal'")
_RLS_TABLES = ("resdex_searches", "resdex_candidates")
_RLS_PREDICATE = (
    "current_setting('app.tenant_id', true) IS NULL "
    "OR current_setting('app.tenant_id', true) = '' "
    "OR current_setting('app.tenant_id', true) = '__all__' "
    "OR tenant_id = current_setting('app.tenant_id', true)"
)


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())

    if "resdex_searches" not in tables:
        op.create_table(
            "resdex_searches",
            sa.Column("tenant_id", sa.String(length=40), nullable=False, server_default=_INTERNAL),
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("run_id", sa.String(length=36), nullable=True),
            sa.Column("job_url", sa.Text(), nullable=False),
            sa.Column("job_title", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("company", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("requirements", sa.JSON(), nullable=True),
            sa.Column("boolean_query", sa.Text(), nullable=False, server_default=""),
            sa.Column("filters", sa.JSON(), nullable=True),
            sa.Column("tier", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("searches_used", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="error"),
            sa.Column("result_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_resdex_searches_tenant_id", "resdex_searches", ["tenant_id"])
        op.create_index("ix_resdex_searches_run_id", "resdex_searches", ["run_id"])
        op.create_index("ix_resdex_searches_job", "resdex_searches", ["tenant_id", "job_url", "created_at"])

    if "resdex_candidates" not in tables:
        op.create_table(
            "resdex_candidates",
            sa.Column("tenant_id", sa.String(length=40), nullable=False, server_default=_INTERNAL),
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("search_id", sa.String(length=36),
                      sa.ForeignKey("resdex_searches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("job_url", sa.Text(), nullable=False),
            sa.Column("rank", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("candidate_name", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("headline", sa.Text(), nullable=False, server_default=""),
            sa.Column("current_designation", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("current_company", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("previous_roles", sa.JSON(), nullable=False),
            sa.Column("experience_text", sa.String(length=60), nullable=False, server_default=""),
            sa.Column("experience_years", sa.Float(), nullable=True),
            sa.Column("location", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("preferred_locations", sa.JSON(), nullable=False),
            sa.Column("skills", sa.JSON(), nullable=False),
            sa.Column("education", sa.Text(), nullable=False, server_default=""),
            sa.Column("notice_period", sa.String(length=60), nullable=False, server_default=""),
            sa.Column("ctc", sa.String(length=60), nullable=False, server_default=""),
            sa.Column("last_active", sa.String(length=60), nullable=False, server_default=""),
            sa.Column("profile_url", sa.Text(), nullable=False, server_default=""),
            sa.Column("matched_skills", sa.JSON(), nullable=False),
            sa.Column("skill_coverage", sa.Float(), nullable=False, server_default="0"),
            sa.Column("extraction_method", sa.String(length=10), nullable=False, server_default=""),
            sa.Column("raw", sa.JSON(), nullable=True),
            sa.Column("outreach_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_resdex_candidates_tenant_id", "resdex_candidates", ["tenant_id"])
        op.create_index("ix_resdex_candidates_search_id", "resdex_candidates", ["search_id"])
        op.create_index("ix_resdex_candidates_outreach_id", "resdex_candidates", ["outreach_id"])

    if "resdex_daily_usage" not in tables:
        op.create_table(
            "resdex_daily_usage",
            sa.Column("usage_date", sa.Date(), primary_key=True),
            sa.Column("searches", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("candidates", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

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
    tables = set(sa_inspect(bind).get_table_names())

    if bind.dialect.name == "postgresql":
        for table in _RLS_TABLES:
            if table in tables:
                bind.execute(sa_text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
    for table in ("resdex_candidates", "resdex_searches", "resdex_daily_usage"):
        if table in tables:
            op.drop_table(table)
