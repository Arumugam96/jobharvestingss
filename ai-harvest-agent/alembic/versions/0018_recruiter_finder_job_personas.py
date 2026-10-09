"""Recruiter Contact Finder: ordered multi-persona on bulk jobs.

Adds recruiter_finder_jobs.personas — the newline-joined, ORDERED list of the "Who to
look up" roles a bulk upload was started with. The enrichment worker walks them
top-to-bottom per company and reveals the first role that yields a contact (one contact
per company). NOT NULL with a '' default so existing jobs and the app's startup
create_all stay consistent.

Idempotent (safe alongside create_all). Portable DDL (PostgreSQL + SQLite).

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-09

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

# revision identifiers, used by Alembic.
revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_TABLE = "recruiter_finder_jobs"
_COLUMN = "personas"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return  # table itself is created by create_all on a fresh DB
    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN not in cols:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN in cols:
        op.drop_column(_TABLE, _COLUMN)
