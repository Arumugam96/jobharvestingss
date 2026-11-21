"""Company classification overrides + editable classification keywords.

Adds two tenant-scoped tables (app/models/company_override.py):

  * company_overrides — one row per (tenant, company_key):
      company_key       — app.core.job_url.normalize_company_key(name); unique per tenant
      company_name      — display name
      hiring_entity / company_size / domain — nullable (NULL = no override)
      source            — "manual" (admin / job edit "apply to company") | "llm"
                          (cached LLM verdict for an Ambiguous company)
      updated_by_email, created_at, updated_at
  * classification_keywords — tenant terms merged into the JSON master lists:
      list ("staffing" | "direct" | "gcc"), term (lower-cased),
      created_by_email, created_at; unique (tenant_id, list, term)

RLS mirrors 0032. Idempotent (safe alongside the app's startup create_all, which
builds these new tables — no ADD COLUMN helper needed). Portable DDL (PostgreSQL +
SQLite).

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-21

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

from alembic import op

# revision identifiers, used by Alembic.
revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

_INTERNAL = sa.text("'internal'")
_OVERRIDES = "company_overrides"
_KEYWORDS = "classification_keywords"
_RLS_PREDICATE = (
    "current_setting('app.tenant_id', true) IS NULL "
    "OR current_setting('app.tenant_id', true) = '' "
    "OR current_setting('app.tenant_id', true) = '__all__' "
    "OR tenant_id = current_setting('app.tenant_id', true)"
)


def _enable_rls(bind, table: str) -> None:
    bind.execute(sa_text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    bind.execute(sa_text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    bind.execute(sa_text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
    bind.execute(sa_text(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING ({_RLS_PREDICATE}) WITH CHECK ({_RLS_PREDICATE})"
    ))


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())

    if _OVERRIDES not in tables:
        op.create_table(
            _OVERRIDES,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("tenant_id", sa.String(length=40), nullable=False, server_default=_INTERNAL),
            sa.Column("company_key", sa.String(length=500), nullable=False),
            sa.Column("company_name", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("hiring_entity", sa.String(length=50), nullable=True),
            sa.Column("company_size", sa.String(length=100), nullable=True),
            sa.Column("domain", sa.String(length=50), nullable=True),
            sa.Column("source", sa.String(length=20), nullable=False, server_default="manual"),
            sa.Column("updated_by_email", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_company_overrides_tenant_id", _OVERRIDES, ["tenant_id"])
        op.create_index(
            "ux_company_overrides_tenant_key", _OVERRIDES, ["tenant_id", "company_key"], unique=True
        )

    if _KEYWORDS not in tables:
        op.create_table(
            _KEYWORDS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("tenant_id", sa.String(length=40), nullable=False, server_default=_INTERNAL),
            sa.Column("list", sa.String(length=20), nullable=False),
            sa.Column("term", sa.String(length=200), nullable=False),
            sa.Column("created_by_email", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_classification_keywords_tenant_id", _KEYWORDS, ["tenant_id"])
        op.create_index(
            "ux_classification_keywords_tenant_list_term", _KEYWORDS,
            ["tenant_id", "list", "term"], unique=True,
        )

    if bind.dialect.name == "postgresql":
        _enable_rls(bind, _OVERRIDES)
        _enable_rls(bind, _KEYWORDS)


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())
    for table in (_KEYWORDS, _OVERRIDES):
        if table not in tables:
            continue
        if bind.dialect.name == "postgresql":
            bind.execute(sa_text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        op.drop_table(table)
