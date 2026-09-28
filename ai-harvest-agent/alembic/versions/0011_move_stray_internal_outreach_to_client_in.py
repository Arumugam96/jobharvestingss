"""Data migration: reassign stray 'internal' email_outreach rows to 'client_in'.

Migration 0010 already moved the pre-multi-tenant email_outreach rows from
'internal' to 'client_in'. But the outreach WRITE paths
(outreach_log_service.build_email_outreach_row and
outreach_routes.log_linkedin_sent) did not stamp tenant_id until this change, so
every outreach row written AFTER 0010 fell back to the column's server default
'internal' regardless of who actually sent it. Those strays are invisible to the
India client (they only surface in the internal all-access view). This sweeps
them to 'client_in', consistent with 0010's decision that outreach carrying no
per-row tenant signal belongs to the India client (Meridian Staffing).

One-shot by design (no mirror in app/main.py's _ensure_* startup helpers, like
0010 — a data move re-run at every boot would wrongly re-tag genuinely-internal
sends made after this point). Idempotent within alembic: because the write path
now stamps the real tenant, a second `upgrade head` matches zero rows. Going
forward, new rows carry the sender's tenant, so this only collects the historical
strays.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-28

"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_TABLE = "email_outreach"
_SRC = "internal"
_DST = "client_in"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return  # brand-new/partial DB — nothing to move
    if "tenant_id" not in {c["name"] for c in inspector.get_columns(_TABLE)}:
        return  # tenant column not present — nothing to move
    bind.execute(
        sa_text(f"UPDATE {_TABLE} SET tenant_id = :dst WHERE tenant_id = :src"),
        {"dst": _DST, "src": _SRC},
    )


def downgrade() -> None:
    # Irreversible by design: after the move, genuinely-new client_in outreach
    # rows are indistinguishable from migrated ones — reversing would steal them.
    pass
