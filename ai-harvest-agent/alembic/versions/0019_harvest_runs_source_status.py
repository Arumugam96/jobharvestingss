"""Per-source live harvest status: harvest_runs.source_status.

Adds harvest_runs.source_status — a nullable JSON map with one entry per ENABLED
source of a multi-source run::

    {"naukri": {"state": "running", "count": 40, "message": "...",
                "reason": "", "error": "", "updated_at": "..."}}

(state ∈ pending|running|done|failed|skipped). Written live by the orchestrator's
SourceStatusTracker (app/services/source_status.py) and served as GET
/harvest-status `sources`, so concurrent sources no longer overwrite each other's
progress message and each source's count rises during the run. Nullable with no
default: runs that predate it (and single-source runs) stay NULL.

JSONB on PostgreSQL, JSON elsewhere (SQLite gives it TEXT affinity). Mirrors the
runtime ADD COLUMN backfill in app/main.py (_ensure_harvest_runs_columns) —
create_all never alters an existing table. Idempotent (safe alongside create_all).

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-10

"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

_TABLE = "harvest_runs"
_COLUMN = "source_status"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return  # table itself is created by create_all on a fresh DB
    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN not in cols:
        json_type = "JSONB" if bind.dialect.name == "postgresql" else "JSON"
        bind.execute(sa_text(f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} {json_type}"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN in cols:
        op.drop_column(_TABLE, _COLUMN)
