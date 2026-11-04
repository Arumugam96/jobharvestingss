"""Call outcome, outcome details and proof on phone_call_logs (the call-log popup).

  * call_outcome    — VARCHAR(40), the outcome under the call status
                      (phone_verification_service.CALL_OUTCOMES); '' for older rows.
  * outcome_details — TEXT, JSON object of the outcome-specific fields.
  * proof           — TEXT, JSON list of proof-of-call files [{name, size, type, src}].

The reminder time reuses follow_up_due_at. Mirrors the runtime ADD COLUMN
backfill in app/main.py (_ensure_phone_call_logs_columns) — create_all never
alters an existing table. Idempotent (safe alongside create_all).

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-04

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from alembic import op

# revision identifiers, used by Alembic.
revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

_TABLE = "phone_call_logs"
_COLUMNS = (
    sa.Column("call_outcome", sa.String(length=40), nullable=False, server_default=""),
    sa.Column("outcome_details", sa.Text(), nullable=False, server_default=""),
    sa.Column("proof", sa.Text(), nullable=False, server_default=""),
)


def upgrade() -> None:
    inspector = sa_inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return  # 0030 not applied yet / create_all will build the full table
    existing = {c["name"] for c in inspector.get_columns(_TABLE)}
    for col in _COLUMNS:
        if col.name not in existing:
            op.add_column(_TABLE, col.copy())


def downgrade() -> None:
    inspector = sa_inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns(_TABLE)}
    for col in reversed(_COLUMNS):
        if col.name in existing:
            op.drop_column(_TABLE, col.name)
