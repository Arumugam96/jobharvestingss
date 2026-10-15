"""Company+location HR shortlist + outreach rotation; recipient lookup index.

Adds, idempotently (safe whether the app's startup create_all / _ensure_* helpers
already ran):
  * company_location_candidates — up to company_contact_shortlist_size HR people per
    (company, job-location) from Apollo's free people search
    (app/models/harvest_run.py::CompanyLocationCandidateORM). Outreach rotates
    through them one person per company+location per day; an email is revealed
    (1 credit) only when the rotation reaches that person.
  * ix_email_outreach_to_email_lower — index on lower(to_email) for the
    one-initial-email-per-recipient-per-day guard (auto_outreach_service).

Mirrors the runtime create_all + _ensure_email_outreach_columns in app/main.py.
Portable DDL — works on PostgreSQL and SQLite alike.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-14

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_TABLE = "company_location_candidates"
_TO_EMAIL_INDEX = "ix_email_outreach_to_email_lower"


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())

    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("company_key", sa.String(length=600), nullable=False),
            sa.Column("location_key", sa.String(length=300), nullable=False),
            sa.Column("apollo_person_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=300), nullable=False, server_default=""),
            sa.Column("title", sa.String(length=300), nullable=False, server_default=""),
            sa.Column("rank", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
            sa.Column("email", sa.String(length=320), nullable=False, server_default=""),
            sa.Column("email_status", sa.String(length=40), nullable=False, server_default=""),
            sa.Column("revealed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("no_email", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
            sa.Column("last_emailed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint(
                "company_key", "location_key", "apollo_person_id",
                name="uq_company_location_candidates_person",
            ),
        )
        op.create_index(
            "ix_company_location_candidates_company_key", _TABLE, ["company_key"],
        )

    if "email_outreach" in tables:
        bind.execute(sa_text(
            f"CREATE INDEX IF NOT EXISTS {_TO_EMAIL_INDEX} ON email_outreach (lower(to_email))"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())

    if "email_outreach" in tables:
        bind.execute(sa_text(f"DROP INDEX IF EXISTS {_TO_EMAIL_INDEX}"))

    if _TABLE in tables:
        op.drop_index("ix_company_location_candidates_company_key", table_name=_TABLE)
        op.drop_table(_TABLE)
