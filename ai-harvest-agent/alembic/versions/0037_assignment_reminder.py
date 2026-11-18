"""Self-assignment reminder dismissal on scraped_jobs.

  * assignment_reminder_done_at — TIMESTAMPTZ, nullable. When the assignee
    dismissed the "still assigned after N days" reminder (due at assigned_at +
    settings.assignment_reminder_days). NULL = not dismissed; reset on every new
    claim, so each assignment gets exactly one reminder.

Mirrors the runtime ADD COLUMN in app/main.py (_ensure_scraped_jobs_columns) —
create_all never alters an existing table. Idempotent (safe alongside create_all).

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-17

"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

_TABLE = "scraped_jobs"
_COLUMN = "assignment_reminder_done_at"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return  # table itself is created by create_all on a fresh DB
    if _COLUMN not in {c["name"] for c in inspector.get_columns(_TABLE)}:
        ts_type = "TIMESTAMPTZ" if bind.dialect.name == "postgresql" else "TIMESTAMP"
        bind.execute(sa_text(f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} {ts_type}"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _COLUMN in {c["name"] for c in inspector.get_columns(_TABLE)}:
        op.drop_column(_TABLE, _COLUMN)
