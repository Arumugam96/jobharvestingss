"""Outreach follow-up threading + per-job company size.

Adds, idempotently (safe whether the app's startup _ensure_* helpers already ran):
  * email_outreach.channel / outreach_kind / parent_outreach_id / provider_message_id
    — channel + follow-up thread linkage + captured provider Message-ID
  * scraped_jobs.company_size — LinkedIn employee-range band captured per job

Mirrors the runtime ADD COLUMN backfills in app/main.py (create_all never alters an
existing table), using portable DDL so this works on PostgreSQL and SQLite alike.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-09

"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


# (table, column, ADD COLUMN DDL) — constant DEFAULTs, portable SQL only.
_ADDS = [
    ("email_outreach", "channel",             "ALTER TABLE email_outreach ADD COLUMN channel VARCHAR(20) NOT NULL DEFAULT 'email'"),
    ("email_outreach", "outreach_kind",       "ALTER TABLE email_outreach ADD COLUMN outreach_kind VARCHAR(20) NOT NULL DEFAULT 'initial'"),
    ("email_outreach", "parent_outreach_id",  "ALTER TABLE email_outreach ADD COLUMN parent_outreach_id VARCHAR(36)"),
    ("email_outreach", "provider_message_id", "ALTER TABLE email_outreach ADD COLUMN provider_message_id VARCHAR(255)"),
    ("scraped_jobs",   "company_size",        "ALTER TABLE scraped_jobs ADD COLUMN company_size VARCHAR(100) NOT NULL DEFAULT ''"),
]

_DROPS = [
    ("email_outreach", "channel"),
    ("email_outreach", "outreach_kind"),
    ("email_outreach", "parent_outreach_id"),
    ("email_outreach", "provider_message_id"),
    ("scraped_jobs",   "company_size"),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())
    for table, column, ddl in _ADDS:
        if table not in tables:
            continue  # brand-new DB — create_all made the table with this column
        existing = {c["name"] for c in inspector.get_columns(table)}
        if column not in existing:
            bind.execute(sa_text(ddl))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())
    for table, column in _DROPS:
        if table not in tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        if column in existing:
            op.drop_column(table, column)
