"""Outreach sent-HTML record.

Adds, idempotently (safe whether the app's startup _ensure_email_outreach_columns
helper already ran), the `email_outreach.body_html` column: the rendered HTML part
as actually delivered (business-card signature, unsubscribe line, job-title link).
`body` keeps only the plain-text source the HTML was built from, so before this
column the log never recorded the signature. Written at send time by
outreach_routes.send_email and auto_outreach_service via
email_service.render_outreach_email_html; NULL on LinkedIn rows and older sends.

Mirrors the runtime ADD COLUMN backfill in app/main.py (create_all never alters an
existing table).

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-25

"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_TABLE = "email_outreach"
_COLUMN = "body_html"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return  # brand-new DB — create_all made the table with this column
    existing = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN not in existing:
        bind.execute(sa_text(f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} TEXT"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    existing = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN in existing:
        op.drop_column(_TABLE, _COLUMN)
