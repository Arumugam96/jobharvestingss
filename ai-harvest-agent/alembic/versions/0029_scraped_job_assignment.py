"""Self-assignment lock on scraped_jobs ("Assign to me" on the Harvested Jobs table).

  * assigned_to_email — VARCHAR(255), nullable; NULL = unassigned. The claiming
    user's email; only that user can open the job's detail or run outreach on it.
  * assigned_to_name  — VARCHAR(255) NOT NULL DEFAULT '' — display name derived
    from the email at claim time.
  * assigned_at       — TIMESTAMPTZ, nullable — when the job was claimed.
  * assignment_note   — TEXT NOT NULL DEFAULT '' — optional comment left on claim.

Mirrors the runtime ADD COLUMN backfill in app/main.py
(_ensure_scraped_jobs_columns) — create_all never alters an existing table.
Idempotent (safe alongside create_all).

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-02

"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

_TABLE = "scraped_jobs"


def _columns(bind) -> dict[str, str]:
    ts_type = "TIMESTAMPTZ" if bind.dialect.name == "postgresql" else "TIMESTAMP"
    return {
        "assigned_to_email": "VARCHAR(255)",
        "assigned_to_name": "VARCHAR(255) NOT NULL DEFAULT ''",
        "assigned_at": ts_type,
        "assignment_note": "TEXT NOT NULL DEFAULT ''",
    }


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return  # table itself is created by create_all on a fresh DB
    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    for name, col_type in _columns(bind).items():
        if name not in cols:
            bind.execute(sa_text(f"ALTER TABLE {_TABLE} ADD COLUMN {name} {col_type}"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    for name in _columns(bind):
        if name in cols:
            op.drop_column(_TABLE, name)
