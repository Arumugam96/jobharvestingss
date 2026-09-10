"""Outreach full event trail.

Adds, idempotently (safe whether the app's startup _ensure_email_outreach_columns
helper already ran), the `email_outreach.events` JSON column that stores the full
ordered list of Mailjet events a send passed through — every event
(sent/open/click/spam/unsub/…) as {"event","at"}, even ones that don't advance the
headline `delivery_status`. Lets the Mail logs UI show every status, not just the
latest. See app/services/outreach_log_service.py::_apply_delivery_event.

Mirrors the runtime ADD COLUMN backfill in app/main.py (create_all never alters an
existing table). Postgres accepts `JSON`; SQLite gives it TEXT affinity — both hold
a JSON-serialised list fine.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-10

"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_TABLE = "email_outreach"
_COLUMN = "events"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return  # brand-new DB — create_all made the table with this column
    existing = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN not in existing:
        bind.execute(sa_text(f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} JSON"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    existing = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN in existing:
        op.drop_column(_TABLE, _COLUMN)
