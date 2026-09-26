"""Data migration: reassign all legacy content rows from 'internal' to 'client_in'.

Everything harvested before client onboarding belongs to the India client
(Meridian Staffing), but rows were stamped with the pre-multi-tenant default
'internal'. Moves the 10 content tables' rows over; 'users' is deliberately
excluded (AuthService rewrites users.tenant_id from the login workspace on every
login, so migrating it would be overwritten noise). companies/user_sessions/
otp_verifications carry no tenant_id.

One-shot by design: there is NO mirror in app/main.py's _ensure_* startup helpers
— a data move re-run at every boot would wrongly re-tag rows internal-tenant
users legitimately create after this migration. Idempotent within alembic: a
second `upgrade head` matches zero rows.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-26

"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

# Mirror of app/main.py::_TENANT_CONTENT_TABLES / 0008::_CONTENT_TABLES.
_CONTENT_TABLES = [
    "harvest_runs",
    "scraped_jobs",
    "llm_calls",
    "reenrichment_tasks",
    "harvest_jobs",
    "harvest_results",
    "email_outreach",
    "recruiters",
    "recruiter_discovery_runs",
    "email_suppressions",
]
_SRC = "internal"
_DST = "client_in"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())
    for table in _CONTENT_TABLES:
        if table not in tables:
            continue  # brand-new/partial DB — nothing to move
        if "tenant_id" not in {c["name"] for c in inspector.get_columns(table)}:
            continue  # tenant column not backfilled yet — nothing to move
        bind.execute(
            sa_text(f"UPDATE {table} SET tenant_id = :dst WHERE tenant_id = :src"),
            {"dst": _DST, "src": _SRC},
        )


def downgrade() -> None:
    # Irreversible by design: after the move, genuinely-new client_in rows are
    # indistinguishable from migrated ones — reversing would steal them.
    pass
