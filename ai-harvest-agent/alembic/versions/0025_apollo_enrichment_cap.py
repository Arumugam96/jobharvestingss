"""Separate Apollo daily cap for contact enrichment: per-purpose usage counter.

Adds, idempotently (safe whether the app's startup create_all already ran):
  * apollo_purpose_usage — per-UTC-day Apollo credit-call counter keyed by
    (usage_date, purpose). Backs the APOLLO_ENRICHMENT_DAILY_CAP sub-cap that only
    harvest/maintenance contact enrichment must pass on top of the global
    apollo_daily_usage ceiling (app/services/apollo_budget.py). Global, not
    tenant-scoped (app/models/harvest_run.py::ApolloPurposeUsageORM).

Mirrors the runtime create_all in app/main.py (new table — no _ensure_* helper).
Portable DDL — works on PostgreSQL and SQLite alike.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-27

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

# revision identifiers, used by Alembic.
revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

_TABLE = "apollo_purpose_usage"


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())

    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("usage_date", sa.Date(), primary_key=True),
            sa.Column("purpose", sa.String(length=20), primary_key=True),
            sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())
    if _TABLE in tables:
        op.drop_table(_TABLE)
