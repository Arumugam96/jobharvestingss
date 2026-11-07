"""Apollo async phones for harvest enrichment + backfill + phone insights sweep.

  * apollo_phone_requests: purpose (finder | enrichment), source (contact_finder |
    linkedin_harvest | … | backfill | unsolicited), linkedin_url.
  * apollo_webhook_events: reprocessed_at, reprocess_note (backfill bookkeeping).
  * recruiters: apollo_person_id (+ index) — maps an un-ref'd delivery to a recruiter.
  * harvest_runs: phone_insights_sent_at (24h phone insights sweep) and apollo_usage
    (added at runtime by main._ensure_harvest_runs_columns in ef589df but never
    mirrored in a migration).

Idempotent; mirrors the app's startup ``_ensure_*`` helpers in app/main.py. Portable
DDL (PostgreSQL + SQLite).

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-07

"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def _columns(bind) -> dict[str, dict[str, str]]:
    pg = bind.dialect.name == "postgresql"
    ts = "TIMESTAMPTZ" if pg else "TIMESTAMP"
    js = "JSONB" if pg else "JSON"
    return {
        "apollo_phone_requests": {
            "purpose": "VARCHAR(20) NOT NULL DEFAULT 'finder'",
            "source": "VARCHAR(40) NOT NULL DEFAULT 'contact_finder'",
            "linkedin_url": "TEXT",
        },
        "apollo_webhook_events": {
            "reprocessed_at": ts,
            "reprocess_note": "TEXT NOT NULL DEFAULT ''",
        },
        "recruiters": {
            "apollo_person_id": "VARCHAR(60)",
        },
        "harvest_runs": {
            "phone_insights_sent_at": ts,
            "apollo_usage": js,
        },
    }


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())
    for table, cols_def in _columns(bind).items():
        if table not in tables:
            continue  # created by create_all on a fresh DB
        cols = {c["name"] for c in inspector.get_columns(table)}
        for name, ddl in cols_def.items():
            if name not in cols:
                bind.execute(sa_text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
    if "recruiters" in tables:
        bind.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS ix_recruiters_apollo_person_id ON recruiters (apollo_person_id)"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())
    if "recruiters" in tables:
        bind.execute(sa_text("DROP INDEX IF EXISTS ix_recruiters_apollo_person_id"))
    for table, cols_def in _columns(bind).items():
        if table not in tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        for name in cols_def:
            # apollo_usage predates this migration at runtime — leave it in place.
            if name in cols and name != "apollo_usage":
                op.drop_column(table, name)
