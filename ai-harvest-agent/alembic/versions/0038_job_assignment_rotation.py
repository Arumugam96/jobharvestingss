"""Round-robin cursor for automatic job assignment to Business Leads.

Adds job_assignment_rotation — one row per tenant (app/models/job_assignment_rotation.py):

  * tenant_id           — primary key.
  * last_assigned_email — lower-cased email of the Business Lead who received the
    tenant's latest auto-assigned job ("" = none yet); the next job goes to the
    first active Business Lead sorting after it.
  * updated_at          — TIMESTAMPTZ.

Scoped by explicit tenant_id in SQL (like recruiter_finder_usage) — no RLS policy.
Idempotent (safe alongside the app's startup create_all, which builds this new
table — no ADD COLUMN helper needed). Portable DDL (PostgreSQL + SQLite).

Revision ID: 0038
Revises: 0037
Create Date: 2026-11-18

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from alembic import op

# revision identifiers, used by Alembic.
revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

_TABLE = "job_assignment_rotation"


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE in set(sa_inspect(bind).get_table_names()):
        return  # create_all (or a prior run) already made it
    op.create_table(
        _TABLE,
        sa.Column("tenant_id", sa.String(length=40), primary_key=True),
        sa.Column("last_assigned_email", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE in set(sa_inspect(bind).get_table_names()):
        op.drop_table(_TABLE)
