"""Harvest run post-harvest phase: harvest_runs.scrape_completed_at + phase_timings.

A harvest run now stays "running" through its post-harvest phase (Resdex search,
auto outreach, the Apollo-contact Resdex sweep) and only gets its final status +
completed_at once that phase ends, so the reported duration covers the whole run.

  * scrape_completed_at — TIMESTAMPTZ, when the scrape finished and its jobs were
    persisted. fail_stale_running closes a run interrupted AFTER this point as
    success (its jobs are saved) instead of failed.
  * phase_timings — JSON map of wall-clock seconds per phase
    ({"scrape_s", "outreach_s", "resdex_sweep_s"}).

Both nullable with no default (older runs stay NULL). JSONB / TIMESTAMPTZ on
PostgreSQL, JSON / TIMESTAMP elsewhere. Mirrors the runtime ADD COLUMN backfill in
app/main.py (_ensure_harvest_runs_columns) — create_all never alters an existing
table. Idempotent (safe alongside create_all).

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-01

"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

_TABLE = "harvest_runs"


def _columns(bind) -> dict[str, str]:
    pg = bind.dialect.name == "postgresql"
    return {
        "scrape_completed_at": "TIMESTAMPTZ" if pg else "TIMESTAMP",
        "phase_timings": "JSONB" if pg else "JSON",
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
