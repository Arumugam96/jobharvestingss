"""Outreach posting-dedup key: email_outreach.job_url.

Adds, idempotently (safe whether the app's startup _ensure_email_outreach_columns
helper already ran), a nullable, indexed `email_outreach.job_url` column holding the
NORMALIZED source posting URL (app.core.job_url.normalize_job_url). It is the
cross-run posting-dedup backstop: auto-outreach skips a send when a prior 'sent'
initial email carries the same key (source-level harvest dedup is the primary
guard; this covers the manual sweep and within-run cross-source overlap).

Mirrors the runtime ADD COLUMN backfill in app/main.py::_ensure_email_outreach_columns
(create_all never alters an existing table).

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-30

"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_TABLE = "email_outreach"
_COLUMN = "job_url"
_INDEX = "ix_email_outreach_job_url"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return  # brand-new DB — create_all made the table with this column + index
    existing = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN not in existing:
        bind.execute(sa_text(f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} VARCHAR(500)"))
    # Portable on PostgreSQL and SQLite (both support IF NOT EXISTS here).
    bind.execute(sa_text(f"CREATE INDEX IF NOT EXISTS {_INDEX} ON {_TABLE} ({_COLUMN})"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    bind.execute(sa_text(f"DROP INDEX IF EXISTS {_INDEX}"))
    existing = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN in existing:
        op.drop_column(_TABLE, _COLUMN)
