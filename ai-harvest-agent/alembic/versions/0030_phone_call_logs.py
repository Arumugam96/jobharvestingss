"""Phone call verification log ("Verify mobile number" on the Harvested Jobs table).

Adds phone_call_logs — one append-only row per call a user logged against a job's
phone number (app/services/phone_verification_service.py). The newest non-cleared
row per job drives the table badge; an Unanswered call with Follow-up on carries
follow_up_due_at, surfaced in the caller's notification bell once due.

  * id               — INTEGER autoincrement PK.
  * job_id           — scraped_jobs.id as a soft link (no FK: a run reconcile
                       deletes and reinserts job rows; the app re-points job_id).
  * follow_up_due_at / follow_up_done_at / cleared_at — TIMESTAMPTZ, nullable.

Tenant-scoped (RLS mirrors 0016/0017). Idempotent (safe alongside the app's startup
create_all). Portable DDL (PostgreSQL + SQLite).

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-03

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

from alembic import op

# revision identifiers, used by Alembic.
revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

_INTERNAL = sa.text("'internal'")
_TABLE = "phone_call_logs"
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
            sa.Column("job_id", sa.String(length=36), nullable=False),
            sa.Column("phone", sa.String(length=50), nullable=False, server_default=""),
            sa.Column("phone_source", sa.String(length=20), nullable=False, server_default=""),
            sa.Column("contact_name", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("call_status", sa.String(length=20), nullable=False),
            sa.Column("remarks", sa.Text(), nullable=False, server_default=""),
            sa.Column("follow_up", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("follow_up_due_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("follow_up_done_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("called_by_email", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("called_by_name", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_phone_call_logs_tenant_id", _TABLE, ["tenant_id"])
        op.create_index("ix_phone_call_logs_job_id", _TABLE, ["job_id"])
        op.create_index("ix_phone_call_logs_tenant_job", _TABLE, ["tenant_id", "job_id"])
        op.create_index(
            "ix_phone_call_logs_caller_due", _TABLE, ["called_by_email", "follow_up_due_at"]
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
