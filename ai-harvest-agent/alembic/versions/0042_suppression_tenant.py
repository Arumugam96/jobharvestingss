"""email_suppressions: unique per (tenant_id, email, reason) — tenant-scoped do-not-call.

A "do_not_call" suppression (a contact's request on a logged call) now applies only
to the tenant whose caller logged it; unsubscribes and terminal bounces stay global
(app/models/suppression.py, app/services/suppression_service.py). Two tenants may
therefore each hold a do-not-call row for one address, and a global bounce row may sit
beside a tenant's do-not-call row, so the old UNIQUE index on email alone goes:

  * ix_email_suppressions_email — recreated as a plain (non-unique) index;
  * ux_email_suppressions_tenant_email_reason — new UNIQUE (tenant_id, email, reason).

tenant_id already exists (0008). Existing rows are unique by email, so the new index
can't conflict. Mirrored by main._ensure_email_suppressions_scope. Idempotent.

Revision ID: 0042
Revises: 0041
Create Date: 2026-11-21

"""
from __future__ import annotations

from sqlalchemy import inspect as sa_inspect

from alembic import op

# revision identifiers, used by Alembic.
revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

_TABLE = "email_suppressions"
_EMAIL_IX = "ix_email_suppressions_email"
_UX = "ux_email_suppressions_tenant_email_reason"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    for ix in inspector.get_indexes(_TABLE):
        if ix["name"] == _EMAIL_IX and ix.get("unique"):
            op.execute(f"DROP INDEX {_EMAIL_IX}")
    if bind.dialect.name == "postgresql":
        for uc in inspector.get_unique_constraints(_TABLE):
            if uc.get("column_names") == ["email"] and uc.get("name"):
                op.execute(f'ALTER TABLE {_TABLE} DROP CONSTRAINT "{uc["name"]}"')
    op.execute(f"CREATE INDEX IF NOT EXISTS {_EMAIL_IX} ON {_TABLE} (email)")
    op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {_UX} ON {_TABLE} (tenant_id, email, reason)")


def downgrade() -> None:
    # Restoring UNIQUE(email) fails once an address has more than one row (two
    # tenants' do-not-call, or do-not-call + bounce) — those must be merged by hand.
    bind = op.get_bind()
    if _TABLE not in sa_inspect(bind).get_table_names():
        return
    op.execute(f"DROP INDEX IF EXISTS {_UX}")
    op.execute(f"DROP INDEX IF EXISTS {_EMAIL_IX}")
    op.execute(f"CREATE UNIQUE INDEX {_EMAIL_IX} ON {_TABLE} (email)")
