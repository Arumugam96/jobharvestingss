"""Job comments (My Workspace "Comments" column).

Adds job_comments (app/models/job_comment.py): a timestamped history of free-text
notes per scraped job, so jobs without a phone number (no call log to carry
remarks — e.g. email-only LinkedIn Feed leads) can hold comments too.

  * tenant_id     — tenant-scoped content table (RLS + apply_tenant).
  * job_id        — soft link to scraped_jobs.id (no FK; reconcile re-inserts rows).
  * body, author_email, author_name, created_at, updated_at (TIMESTAMPTZ).

Idempotent (safe alongside the app's startup create_all, which builds this new
table — no ADD COLUMN helper needed). Portable DDL (PostgreSQL + SQLite).

Revision ID: 0043
Revises: 0042
Create Date: 2026-11-25

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from alembic import op

# revision identifiers, used by Alembic.
revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

_TABLE = "job_comments"


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE in set(sa_inspect(bind).get_table_names()):
        return  # create_all (or a prior run) already made it
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(length=40), nullable=False, server_default="internal"),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("author_email", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("author_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_job_comments_tenant_id", _TABLE, ["tenant_id"])
    op.create_index("ix_job_comments_job_id", _TABLE, ["job_id"])
    op.create_index("ix_job_comments_job_created", _TABLE, ["job_id", "created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE in set(sa_inspect(bind).get_table_names()):
        op.drop_table(_TABLE)
