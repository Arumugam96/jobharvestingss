"""Apollo credit snapshots: point-in-time readings of Apollo's own credit counter.

Adds, idempotently (safe whether the app's startup create_all already ran):
  * apollo_credit_snapshots — one row per read of POST /usage_stats/credit_usage_stats
    (``lead_credit`` consumed / limit + the billing-cycle start), taken every 15 min and
    at each harvest run's start. The harvest report diffs readings for true "credits
    today" / "credits this run" (app/services/apollo_credit_usage.py). Global, not
    tenant-scoped (app/models/harvest_run.py::ApolloCreditSnapshotORM).

Mirrors the runtime create_all in app/main.py (new table — no _ensure_* helper).
Portable DDL — works on PostgreSQL and SQLite alike.

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-02

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

# revision identifiers, used by Alembic.
revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

_TABLE = "apollo_credit_snapshots"
_INDEX = "ix_apollo_credit_snapshots_taken_at"


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())

    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "taken_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("consumed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("credit_limit", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cycle_start", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(_INDEX, _TABLE, ["taken_at"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())
    if _TABLE in tables:
        op.drop_index(_INDEX, table_name=_TABLE)
        op.drop_table(_TABLE)
