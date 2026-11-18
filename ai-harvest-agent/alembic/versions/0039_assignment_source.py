"""Record how a scraped job got its assignee.

Adds scraped_jobs.assignment_source (VARCHAR(20) NOT NULL DEFAULT ''):

  * ""       — a manual "Assign to me" claim (and every assignment made before
               this column existed).
  * "auto"   — round-robin auto-assignment to a Business Lead
               (app/services/auto_assignment_service.py).
  * "demand" — a Demand Search job given to the Business Lead who ran the search.

Automated post-harvest outreach skips only manual claims, so auto-assigned jobs
still get the initial email (app/services/auto_outreach_service.py).

Mirrored by main._ensure_scraped_jobs_columns (the app adds the column at startup).
Idempotent. Portable DDL (PostgreSQL + SQLite).

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-18

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from alembic import op

# revision identifiers, used by Alembic.
revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa_inspect(bind).get_columns("scraped_jobs")}
    if "assignment_source" not in cols:
        op.add_column(
            "scraped_jobs",
            sa.Column("assignment_source", sa.String(length=20), nullable=False, server_default=""),
        )


def downgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa_inspect(bind).get_columns("scraped_jobs")}
    if "assignment_source" in cols:
        op.drop_column("scraped_jobs", "assignment_source")
