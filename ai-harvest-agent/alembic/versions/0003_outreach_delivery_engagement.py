"""Outreach delivery engagement (Mailjet event tracking).

Adds, idempotently (safe whether the app's startup _ensure_email_outreach_columns
helper already ran), the per-send delivery-engagement columns updated by Mailjet's
event webhook (app/routes/outreach_routes.py::mailjet_events):

  * email_outreach.delivery_status — furthest-along state: sent → delivered →
    opened → clicked, or bounced/blocked/spam (NULL until the first event)
  * email_outreach.delivered_at / opened_at / bounced_at — event timestamps
  * email_outreach.replied_at — reserved for the later inbound-reply phase

Mirrors the runtime ADD COLUMN backfill in app/main.py (create_all never alters an
existing table). PostgreSQL uses TIMESTAMPTZ; SQLite ignores type affinity so a
plain TIMESTAMP is fine there.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-10

"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_TABLE = "email_outreach"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return  # brand-new DB — create_all made the table with these columns
    ts_type = "TIMESTAMPTZ" if bind.dialect.name == "postgresql" else "TIMESTAMP"
    adds = [
        ("delivery_status", f"ALTER TABLE {_TABLE} ADD COLUMN delivery_status VARCHAR(20)"),
        ("delivered_at",    f"ALTER TABLE {_TABLE} ADD COLUMN delivered_at {ts_type}"),
        ("opened_at",       f"ALTER TABLE {_TABLE} ADD COLUMN opened_at {ts_type}"),
        ("bounced_at",      f"ALTER TABLE {_TABLE} ADD COLUMN bounced_at {ts_type}"),
        ("replied_at",      f"ALTER TABLE {_TABLE} ADD COLUMN replied_at {ts_type}"),
    ]
    existing = {c["name"] for c in inspector.get_columns(_TABLE)}
    for column, ddl in adds:
        if column not in existing:
            bind.execute(sa_text(ddl))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    existing = {c["name"] for c in inspector.get_columns(_TABLE)}
    for column in ("delivery_status", "delivered_at", "opened_at", "bounced_at", "replied_at"):
        if column in existing:
            op.drop_column(_TABLE, column)
