"""All three Resdex tier queries on resdex_searches.

  * tier_queries — JSON, nullable. [{"tier", "query", "filters"}] for T1..T3, built
                   from the search's requirements. A queries-only search (recruiter
                   logged out during the harvest) keeps all three, and the job page's
                   "Run Resdex search" later runs exactly these.

Mirrors the runtime ADD COLUMN in app/main.py (_ensure_resdex_searches_columns) —
create_all never alters an existing table. Idempotent (safe alongside create_all).

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-13

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from alembic import op

# revision identifiers, used by Alembic.
revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

_TABLE = "resdex_searches"
_COLUMN = sa.Column("tier_queries", sa.JSON(), nullable=True)


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
