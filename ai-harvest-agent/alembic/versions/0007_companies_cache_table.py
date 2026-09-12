"""Company-level enrichment cache table.

Creates the `companies` table (app/models/harvest_run.py::CompanyORM) that caches
each company's Apollo-enriched size / HQ location, keyed by a normalized company
name, so the data is fetched once and applied to every job of that company —
including jobs with no recruiter. Idempotent: create_all already makes this table
on a brand-new DB at startup, so this only creates it where it's missing.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-11

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

# revision identifiers, used by Alembic.
revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if "companies" in set(inspector.get_table_names()):
        return  # create_all (or a prior run) already made it
    op.create_table(
        "companies",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("company_key", sa.String(length=600), nullable=False),
        sa.Column("company_name", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("domain", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("company_size", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("company_country", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("company_state", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("company_industry", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("apollo_attempted", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("apollo_enriched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_companies_company_key", "companies", ["company_key"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if "companies" not in set(inspector.get_table_names()):
        return
    op.drop_index("ix_companies_company_key", table_name="companies")
    op.drop_table("companies")
