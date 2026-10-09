"""Recruiter Contact Finder: reveal log (History tab).

Adds recruiter_finder_reveal_log — one append-only row per Apollo contact ACTUALLY
revealed from the Single-search section (company browse or specific-person). Powers the
new History tab: who revealed what, when, reveal type, credits, plus the richer Apollo
fields (secondary email, org industry/size, precise location) the deduped `recruiters`
record doesn't keep. Tenant-scoped (RLS mirrors 0016).

Idempotent (safe alongside the app's startup create_all). Portable DDL (PostgreSQL + SQLite).

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-08

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_INTERNAL = sa.text("'internal'")
_TABLE = "recruiter_finder_reveal_log"
_RLS_PREDICATE = (
    "current_setting('app.tenant_id', true) IS NULL "
    "OR current_setting('app.tenant_id', true) = '' "
    "OR current_setting('app.tenant_id', true) = '__all__' "
    "OR tenant_id = current_setting('app.tenant_id', true)"
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("tenant_id", sa.String(length=40), nullable=False, server_default=_INTERNAL),
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("person_id", sa.String(length=60), nullable=False, server_default=""),
            sa.Column("contact_name", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("contact_title", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("company", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("company_domain", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("email", sa.String(length=320), nullable=False, server_default=""),
            sa.Column("email_status", sa.String(length=30), nullable=False, server_default=""),
            sa.Column("secondary_email", sa.String(length=320), nullable=False, server_default=""),
            sa.Column("phone", sa.String(length=50), nullable=False, server_default=""),
            sa.Column("phone_status", sa.String(length=20), nullable=False, server_default=""),
            sa.Column("linkedin_url", sa.Text(), nullable=False, server_default=""),
            sa.Column("city", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("state", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("country", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("industry", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("company_size", sa.Integer(), nullable=True),
            sa.Column("confidence", sa.String(length=20), nullable=False, server_default=""),
            sa.Column("reveal_type", sa.String(length=10), nullable=False, server_default="email"),
            sa.Column("credits_spent", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("source", sa.String(length=30), nullable=False, server_default=""),
            sa.Column("requested_by", sa.String(length=120), nullable=True),
            sa.Column("recruiter_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_recruiter_finder_reveal_log_tenant_id", _TABLE, ["tenant_id"])
        op.create_index("ix_recruiter_finder_reveal_log_created_at", _TABLE, ["created_at"])
        op.create_index("ix_rf_reveal_log_tenant_created", _TABLE, ["tenant_id", "created_at"])

    if bind.dialect.name == "postgresql":
        bind.execute(sa_text(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY"))
        bind.execute(sa_text(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY"))
        bind.execute(sa_text(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}"))
        bind.execute(sa_text(
            f"CREATE POLICY tenant_isolation ON {_TABLE} "
            f"USING ({_RLS_PREDICATE}) WITH CHECK ({_RLS_PREDICATE})"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    if bind.dialect.name == "postgresql" and _TABLE in tables:
        bind.execute(sa_text(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}"))
    if _TABLE in tables:
        op.drop_table(_TABLE)
