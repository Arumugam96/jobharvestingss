"""Phone-level flags set by logged calls (the call-log popup's side effects).

Adds phone_contact_flags — one row per (tenant, number) holding what calls learned
about it (app/models/phone_contact_flag.py):

  * phone_key        — the number's digits only; unique per tenant.
  * invalid_at / dnc_at / warm_at / reenrich_requested_at — TIMESTAMPTZ, nullable.
  * correct_number   — the number given on a wrong-number call.
  * last_call_id     — phone_call_logs.id as a soft link.

Tenant-scoped (RLS mirrors 0030). Idempotent (safe alongside the app's startup
create_all, which builds this new table — no ADD COLUMN helper needed). Portable DDL
(PostgreSQL + SQLite).

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-06

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

from alembic import op

# revision identifiers, used by Alembic.
revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

_INTERNAL = sa.text("'internal'")
_TABLE = "phone_contact_flags"
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
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("tenant_id", sa.String(length=40), nullable=False, server_default=_INTERNAL),
            sa.Column("phone_key", sa.String(length=50), nullable=False),
            sa.Column("phone", sa.String(length=50), nullable=False, server_default=""),
            sa.Column("invalid_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("dnc_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("warm_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reenrich_requested_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("correct_number", sa.String(length=50), nullable=False, server_default=""),
            sa.Column("last_outcome", sa.String(length=40), nullable=False, server_default=""),
            sa.Column("last_call_id", sa.Integer(), nullable=True),
            sa.Column("updated_by_email", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_phone_contact_flags_tenant_id", _TABLE, ["tenant_id"])
        op.create_index(
            "ux_phone_contact_flags_tenant_phone", _TABLE, ["tenant_id", "phone_key"], unique=True
        )

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
