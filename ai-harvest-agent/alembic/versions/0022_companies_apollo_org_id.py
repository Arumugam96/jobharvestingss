"""Cached Apollo organization id on companies.

Adds companies.apollo_org_id + companies.apollo_org_resolved_at. Resolving a company
to an Apollo organization (organizations/enrich, mixed_companies/search) costs a
credit, so the id is resolved once per company and reused by every (company,
location) HR shortlist, the company-details pass and the Recruiter Finder
(app/services/company_enrichment_service.resolve_apollo_org). The timestamp stamps
every attempt (hit or miss) so an unknown company isn't re-billed within
apollo_recheck_days.

Mirrors the runtime ADD COLUMN backfill in app/main.py (_ensure_companies_columns) —
create_all never alters an existing table. Idempotent (safe alongside create_all).
Portable DDL (PostgreSQL + SQLite).

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-15

"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

_TABLE = "companies"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return  # table itself is created by create_all on a fresh DB
    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    ts_type = "TIMESTAMPTZ" if bind.dialect.name == "postgresql" else "TIMESTAMP"
    if "apollo_org_id" not in cols:
        bind.execute(sa_text(
            f"ALTER TABLE {_TABLE} ADD COLUMN apollo_org_id VARCHAR(64) NOT NULL DEFAULT ''"
        ))
    if "apollo_org_resolved_at" not in cols:
        bind.execute(sa_text(f"ALTER TABLE {_TABLE} ADD COLUMN apollo_org_resolved_at {ts_type}"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    for col in ("apollo_org_resolved_at", "apollo_org_id"):
        if col in cols:
            op.drop_column(_TABLE, col)
