"""Inbound outreach replies: the outreach_replies table + RLS.

Adds the table that stores prospect replies captured from the Brevo inbound-parse
webhook (app/models/outreach_reply.py), soft-linked to the send they answer. The
matched send's replied_at (already present since 0003) is stamped by the webhook;
no DDL needed for that column here.

Idempotent and safe whether the app's startup create_all already made the table
(create_all creates brand-new tables — unlike the _ensure_* column backfills, which
exist only because create_all never ALTERs an existing table). On PostgreSQL the
table also gets the same tenant RLS policy as the other content tables
(app/main.py::_ensure_rls_policies), so cross-tenant rows are refused by the DB.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-01

"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_TABLE = "outreach_replies"


def _ts_type(bind) -> str:
    return "TIMESTAMPTZ" if bind.dialect.name == "postgresql" else "TIMESTAMP"


def _rls_predicate() -> str:
    return (
        "current_setting('app.tenant_id', true) IS NULL "
        "OR current_setting('app.tenant_id', true) = '' "
        "OR current_setting('app.tenant_id', true) = '__all__' "
        "OR tenant_id = current_setting('app.tenant_id', true)"
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    ts = _ts_type(bind)
    is_pg = bind.dialect.name == "postgresql"
    bool_default = "false"

    if _TABLE not in set(inspector.get_table_names()):
        bind.execute(sa_text(
            f"""
            CREATE TABLE {_TABLE} (
                id            VARCHAR(36) PRIMARY KEY,
                tenant_id     VARCHAR(40) NOT NULL DEFAULT 'internal',
                outreach_id   VARCHAR(36),
                job_id        VARCHAR(64),
                recruiter_id  VARCHAR(36),
                from_email    VARCHAR(255) NOT NULL DEFAULT '',
                from_name     VARCHAR(255) NOT NULL DEFAULT '',
                to_email      VARCHAR(255) NOT NULL DEFAULT '',
                company       VARCHAR(255) NOT NULL DEFAULT '',
                subject       TEXT NOT NULL DEFAULT '',
                body          TEXT NOT NULL DEFAULT '',
                body_html     TEXT,
                message_id    VARCHAR(255),
                in_reply_to   TEXT,
                is_read       BOOLEAN NOT NULL DEFAULT {bool_default},
                forwarded     BOOLEAN NOT NULL DEFAULT {bool_default},
                received_at   {ts},
                created_at    {ts} DEFAULT now(),
                raw           JSON
            )
            """
        ))

    # Indexes (IF NOT EXISTS → portable on PostgreSQL + SQLite >= 3.8); match the
    # model's ix_<table>_<col> naming so a fresh create_all is a no-op here.
    for col in ("tenant_id", "outreach_id", "job_id", "recruiter_id", "is_read"):
        bind.execute(sa_text(
            f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_{col} ON {_TABLE} ({col})"
        ))

    if is_pg:
        predicate = _rls_predicate()
        bind.execute(sa_text(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY"))
        bind.execute(sa_text(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY"))
        bind.execute(sa_text(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}"))
        bind.execute(sa_text(
            f"CREATE POLICY tenant_isolation ON {_TABLE} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE in set(sa_inspect(bind).get_table_names()):
        op.drop_table(_TABLE)
