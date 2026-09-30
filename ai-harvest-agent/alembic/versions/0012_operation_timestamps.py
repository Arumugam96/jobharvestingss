"""Operation-time timestamps: scraped_jobs.scraped_at + email_outreach.sent_at.

Adds, idempotently (safe whether the app's startup _ensure_* helpers already ran),
two nullable timestamp columns that record when an operation actually happened,
distinct from the row's insert time (created_at, a server default):

  * scraped_jobs.scraped_at — the real scrape/collection instant, stamped in the
    orchestrator converters (app/agents/orchestrator_agent.py). created_at is the
    batch-insert time, which on a multi-hour run lags the scrape.
  * email_outreach.sent_at  — the real send-attempt instant, recorded for BOTH
    sent and failed rows (unlike delivered_at, which is success-only).

Both are nullable with no server default (the app populates them), so existing rows
stay NULL rather than needing a constant backfill value.

The related llm_calls.called_at and email_suppressions.unsubscribed_at columns
already exist (with server_default=func.now()); this release only changes them to be
APP-populated with the real operation time, so they need no DDL.

Mirrors the runtime ADD COLUMN backfills in app/main.py
(_ensure_scraped_jobs_columns / _ensure_email_outreach_columns) — create_all never
alters an existing table.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-30

"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

# (table, column) pairs to add as nullable, app-populated timestamps.
_ADDS = [
    ("scraped_jobs", "scraped_at"),
    ("email_outreach", "sent_at"),
]


def _ts_type(bind) -> str:
    # Postgres wants TIMESTAMPTZ to match DateTime(timezone=True); SQLite ignores
    # the type affinity, so a plain TIMESTAMP is fine there.
    return "TIMESTAMPTZ" if bind.dialect.name == "postgresql" else "TIMESTAMP"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())
    ts_type = _ts_type(bind)
    for table, column in _ADDS:
        if table not in tables:
            continue  # brand-new DB — create_all made the table with this column
        existing = {c["name"] for c in inspector.get_columns(table)}
        if column not in existing:
            bind.execute(sa_text(f"ALTER TABLE {table} ADD COLUMN {column} {ts_type}"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())
    for table, column in _ADDS:
        if table not in tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        if column in existing:
            op.drop_column(table, column)
