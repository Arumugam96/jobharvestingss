"""Per-job location + company-location columns on scraped_jobs (+ recruiters).

Adds, idempotently (safe whether the app's startup _ensure_* helpers already ran):
  * scraped_jobs.country / state         — job-location country/state parsed from
    the free-text `location` (app/core/location.py); powers a display-time
    Country filter/facet.
  * scraped_jobs.company_country / company_state — company HQ location, merged in
    from the recruiter's Apollo org data; powers a "Company country" filter/facet.
  * recruiters.company_country / company_state    — company HQ location from the
    Apollo organization match (apollo_enrichment.py), the source of the job's
    company_country/company_state above.

Mirrors the runtime ADD COLUMN backfills in app/main.py::_ensure_scraped_jobs_columns
and _ensure_recruiter_columns (create_all never alters an existing table), using
portable DDL so this works on PostgreSQL and SQLite alike.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-11

"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


# (table, column, ADD COLUMN DDL) — constant DEFAULTs, portable SQL only.
_ADDS = [
    ("scraped_jobs", "country",         "ALTER TABLE scraped_jobs ADD COLUMN country VARCHAR(100) NOT NULL DEFAULT ''"),
    ("scraped_jobs", "state",           "ALTER TABLE scraped_jobs ADD COLUMN state VARCHAR(100) NOT NULL DEFAULT ''"),
    ("scraped_jobs", "company_country", "ALTER TABLE scraped_jobs ADD COLUMN company_country VARCHAR(100) NOT NULL DEFAULT ''"),
    ("scraped_jobs", "company_state",   "ALTER TABLE scraped_jobs ADD COLUMN company_state VARCHAR(100) NOT NULL DEFAULT ''"),
    ("recruiters",   "company_state",   "ALTER TABLE recruiters ADD COLUMN company_state VARCHAR(120) NOT NULL DEFAULT ''"),
    ("recruiters",   "company_country", "ALTER TABLE recruiters ADD COLUMN company_country VARCHAR(120) NOT NULL DEFAULT ''"),
]

_DROPS = [
    ("scraped_jobs", "country"),
    ("scraped_jobs", "state"),
    ("scraped_jobs", "company_country"),
    ("scraped_jobs", "company_state"),
    ("recruiters",   "company_state"),
    ("recruiters",   "company_country"),
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
