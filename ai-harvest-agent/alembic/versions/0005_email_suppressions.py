"""Outreach suppression list + recruiter opt-out mirror.

Adds, idempotently (safe whether the app's startup create_all / _ensure_recruiter_columns
already ran):
  * email_suppressions — the email-keyed do-not-contact source of truth (unsubscribe)
  * recruiters.unsubscribed / recruiters.unsubscribed_at — CRM-visible mirror flag

Mirrors the runtime create_all + ADD COLUMN backfill in app/main.py. Uses portable
DDL so it works on PostgreSQL and SQLite alike.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-10

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    if "email_suppressions" not in tables:
        op.create_table(
            "email_suppressions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("email", sa.String(length=255), nullable=False),
            sa.Column("reason", sa.String(length=30), nullable=False, server_default="unsubscribe"),
            sa.Column("source", sa.String(length=30), nullable=False, server_default=""),
            sa.Column("recruiter_id", sa.String(length=36), nullable=True),
            sa.Column("raw_payload", sa.JSON(), nullable=True),
            sa.Column("unsubscribed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_email_suppressions_email", "email_suppressions", ["email"], unique=True)
        op.create_index("ix_email_suppressions_recruiter_id", "email_suppressions", ["recruiter_id"])

    if "recruiters" in tables:
        ts_type = "TIMESTAMPTZ" if bind.dialect.name == "postgresql" else "TIMESTAMP"
        existing = {c["name"] for c in inspector.get_columns("recruiters")}
        if "unsubscribed" not in existing:
            bind.execute(sa_text("ALTER TABLE recruiters ADD COLUMN unsubscribed BOOLEAN NOT NULL DEFAULT FALSE"))
        if "unsubscribed_at" not in existing:
            bind.execute(sa_text(f"ALTER TABLE recruiters ADD COLUMN unsubscribed_at {ts_type}"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())
    if "recruiters" in tables:
        existing = {c["name"] for c in inspector.get_columns("recruiters")}
        for col in ("unsubscribed_at", "unsubscribed"):
            if col in existing:
                op.drop_column("recruiters", col)
    if "email_suppressions" in tables:
        op.drop_table("email_suppressions")
