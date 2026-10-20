"""Resdex for Apollo-enriched contacts.

Adds, idempotently (safe whether the app's startup create_all / _ensure_* helpers
already ran):
  * resdex_searches.contact_source — origin of the outreach contact that triggered the
    search (job_post | recruiter_profile | apollo | apollo_hr; "" for older rows / jobs
    with no contact), shown as a badge on the job detail page.
  * resdex_candidate_emails — one row per (candidate, outreach email) that carried it,
    so a reused match emailed to a later contact (e.g. the next Apollo HR contact)
    doesn't take the candidates away from the earlier email on the job detail page
    (app/models/resdex.py::ResdexCandidateEmailORM). Tenant-scoped.

Mirrors the runtime create_all + _ensure_resdex_searches_columns in app/main.py.
Portable DDL — works on PostgreSQL and SQLite alike.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-19

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

_SEARCHES = "resdex_searches"
_LINKS = "resdex_candidate_emails"


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())

    if _SEARCHES in tables:
        cols = {c["name"] for c in sa_inspect(bind).get_columns(_SEARCHES)}
        if "contact_source" not in cols:
            bind.execute(sa_text(
                f"ALTER TABLE {_SEARCHES} ADD COLUMN contact_source VARCHAR(30) NOT NULL DEFAULT ''"
            ))

    if _LINKS not in tables:
        op.create_table(
            _LINKS,
            sa.Column("tenant_id", sa.String(length=40), nullable=False,
                      server_default=sa.text("'internal'")),
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "candidate_id", sa.String(length=36),
                sa.ForeignKey("resdex_candidates.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("outreach_id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("candidate_id", "outreach_id", name="uq_resdex_candidate_emails"),
        )
        op.create_index("ix_resdex_candidate_emails_tenant_id", _LINKS, ["tenant_id"])
        op.create_index("ix_resdex_candidate_emails_candidate_id", _LINKS, ["candidate_id"])
        op.create_index("ix_resdex_candidate_emails_outreach_id", _LINKS, ["outreach_id"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())
    if _LINKS in tables:
        op.drop_table(_LINKS)
    if _SEARCHES in tables:
        cols = {c["name"] for c in sa_inspect(bind).get_columns(_SEARCHES)}
        if "contact_source" in cols:
            op.drop_column(_SEARCHES, "contact_source")
