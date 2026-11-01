"""Apollo credit ledger: log of credits ACTUALLY consumed per call.

Adds, idempotently (safe whether the app's startup create_all already ran):
  * apollo_credit_ledger — one row per Apollo call that really consumed credits (the
    response's ``credits_consumed`` when reported, else the billable records returned).
    The daily counters (apollo_daily_usage / apollo_purpose_usage) are now pre-flight
    reservations settled down to this real charge (app/services/apollo_budget.py::settle);
    the ledger's timestamps give a harvest run its "credits this run" figure
    (apollo_budget.credits_between). Global, not tenant-scoped
    (app/models/harvest_run.py::ApolloCreditLedgerORM).

Mirrors the runtime create_all in app/main.py (new table — no _ensure_* helper).
Portable DDL — works on PostgreSQL and SQLite alike.

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-01

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

# revision identifiers, used by Alembic.
revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

_TABLE = "apollo_credit_ledger"
_INDEX = "ix_apollo_credit_ledger_created_at"


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())

    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("purpose", sa.String(length=20), nullable=False, server_default=""),
            sa.Column("endpoint", sa.String(length=60), nullable=False, server_default=""),
            sa.Column("credits", sa.Integer(), nullable=False, server_default="0"),
        )
        op.create_index(_INDEX, _TABLE, ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())
    if _TABLE in tables:
        op.drop_index(_INDEX, table_name=_TABLE)
        op.drop_table(_TABLE)
