"""User role on users ("user" | "admin").

  * role — VARCHAR(20) NOT NULL DEFAULT 'user'. Admins read call logs on jobs
           self-assigned by others (Phone logs page, table badges) and manage roles.

Mirrors the runtime ADD COLUMN in app/main.py (_ensure_users_role_column) —
create_all never alters an existing table. Idempotent (safe alongside create_all).

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-12

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from alembic import op

# revision identifiers, used by Alembic.
revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

_TABLE = "users"
_COLUMN = sa.Column("role", sa.String(length=20), nullable=False, server_default="user")


def upgrade() -> None:
    inspector = sa_inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    if _COLUMN.name not in {c["name"] for c in inspector.get_columns(_TABLE)}:
        op.add_column(_TABLE, _COLUMN.copy())


def downgrade() -> None:
    inspector = sa_inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    if _COLUMN.name in {c["name"] for c in inspector.get_columns(_TABLE)}:
        op.drop_column(_TABLE, _COLUMN.name)
