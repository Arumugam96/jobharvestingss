"""Company+location HR-contact fallback: separate job contact columns,
location-contact cache table, and the global Apollo daily-usage counter.

Adds, idempotently (safe whether the app's startup create_all / _ensure_* helpers
already ran):
  * scraped_jobs.company_contact_{name,title,email,phone,location,source} — a
    location-specific HR contact discovered via Apollo when the job had no
    recruiter/poster email, stored SEPARATELY so the original poster fields are
    never overwritten.
  * company_location_contacts — the (company, job-location)-keyed HR-contact cache
    (app/models/harvest_run.py::CompanyLocationContactORM).
  * apollo_daily_usage — the global per-UTC-day Apollo credit-call counter backing
    the hard daily cap (app/services/apollo_budget.py).

Mirrors the runtime create_all + _ensure_scraped_jobs_columns backfills in
app/main.py (create_all never alters an existing table), using portable DDL so this
works on PostgreSQL and SQLite alike.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-06

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


# (column, ADD COLUMN DDL) — constant DEFAULTs, portable SQL only.
_JOB_ADDS = [
    ("company_contact_name",     "ALTER TABLE scraped_jobs ADD COLUMN company_contact_name TEXT NOT NULL DEFAULT ''"),
    ("company_contact_title",    "ALTER TABLE scraped_jobs ADD COLUMN company_contact_title TEXT NOT NULL DEFAULT ''"),
    ("company_contact_email",    "ALTER TABLE scraped_jobs ADD COLUMN company_contact_email TEXT NOT NULL DEFAULT ''"),
    ("company_contact_phone",    "ALTER TABLE scraped_jobs ADD COLUMN company_contact_phone VARCHAR(50) NOT NULL DEFAULT ''"),
    ("company_contact_location", "ALTER TABLE scraped_jobs ADD COLUMN company_contact_location TEXT NOT NULL DEFAULT ''"),
    ("company_contact_source",   "ALTER TABLE scraped_jobs ADD COLUMN company_contact_source VARCHAR(30) NOT NULL DEFAULT ''"),
]

_JOB_DROPS = [col for col, _ in _JOB_ADDS]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    # ── scraped_jobs company_contact_* columns ──
    if "scraped_jobs" in tables:
        existing = {c["name"] for c in inspector.get_columns("scraped_jobs")}
        for column, ddl in _JOB_ADDS:
            if column not in existing:
                bind.execute(sa_text(ddl))

    # ── company_location_contacts cache table ──
    if "company_location_contacts" not in tables:
        op.create_table(
            "company_location_contacts",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("company_key", sa.String(length=600), nullable=False),
            sa.Column("location_key", sa.String(length=300), nullable=False),
            sa.Column("company_name", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("location", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("domain", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("hr_contact_name", sa.String(length=300), nullable=False, server_default=""),
            sa.Column("hr_contact_title", sa.String(length=300), nullable=False, server_default=""),
            sa.Column("hr_contact_email", sa.String(length=320), nullable=False, server_default=""),
            sa.Column("hr_contact_phone", sa.String(length=50), nullable=False, server_default=""),
            sa.Column("apollo_attempted", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
            sa.Column("hr_contact_attempted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("company_key", "location_key", name="uq_company_location_contacts_keys"),
        )
        op.create_index(
            "ix_company_location_contacts_company_key",
            "company_location_contacts", ["company_key"],
        )
        op.create_index(
            "ix_company_location_contacts_location_key",
            "company_location_contacts", ["location_key"],
        )

    # ── apollo_daily_usage global counter table ──
    if "apollo_daily_usage" not in tables:
        op.create_table(
            "apollo_daily_usage",
            sa.Column("usage_date", sa.Date(), primary_key=True),
            sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    if "apollo_daily_usage" in tables:
        op.drop_table("apollo_daily_usage")

    if "company_location_contacts" in tables:
        op.drop_index("ix_company_location_contacts_location_key", table_name="company_location_contacts")
        op.drop_index("ix_company_location_contacts_company_key", table_name="company_location_contacts")
        op.drop_table("company_location_contacts")

    if "scraped_jobs" in tables:
        existing = {c["name"] for c in inspector.get_columns("scraped_jobs")}
        for column in _JOB_DROPS:
            if column in existing:
                op.drop_column("scraped_jobs", column)
