"""Multi-tenancy: tenants table, tenant_id on content tables, RLS policies.

Mirrors the runtime helpers in app/main.py (_ensure_tenant_columns /
_ensure_tenants_seeded / _ensure_rls_policies) — the app applies schema via
create_all + those idempotent helpers at startup, and this revision is the
Alembic-parity equivalent (same portable, live-DB-safe approach as 0006).

Adds, idempotently:
  * tenants                 — the workspace/tenant entity + seed rows.
  * <content>.tenant_id     — owning tenant on every content table + users,
    backfilled to 'internal' via the column DEFAULT, then indexed.
  * RLS policies (Postgres) — per content table, permissive when app.tenant_id
    is unset/'' and all-access for the '__all__' sentinel.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-25
"""
from __future__ import annotations

import json

from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

from app.models.tenant import SEED_TENANTS

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_CONTENT_TABLES = [
    "harvest_runs", "scraped_jobs", "llm_calls", "reenrichment_tasks",
    "harvest_jobs", "harvest_results", "email_outreach",
    "recruiters", "recruiter_discovery_runs", "email_suppressions",
]
_RLS_PREDICATE = (
    "current_setting('app.tenant_id', true) IS NULL "
    "OR current_setting('app.tenant_id', true) = '' "
    "OR current_setting('app.tenant_id', true) = '__all__' "
    "OR tenant_id = current_setting('app.tenant_id', true)"
)


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    # 1. tenants table
    if "tenants" not in tables:
        json_type = "JSON" if is_pg else "TEXT"
        ts_type = "TIMESTAMPTZ" if is_pg else "TIMESTAMP"
        bind.execute(sa_text(
            "CREATE TABLE tenants ("
            "id VARCHAR(40) PRIMARY KEY, "
            "name VARCHAR(200) NOT NULL DEFAULT '', "
            "type VARCHAR(20) NOT NULL DEFAULT 'client', "
            "region VARCHAR(10) NOT NULL DEFAULT '', "
            f"config {json_type} NOT NULL, "
            "is_active BOOLEAN NOT NULL DEFAULT TRUE, "
            f"created_at {ts_type}, updated_at {ts_type}"
            ")"
        ))

    # 2. seed tenants
    config_ph = "CAST(:config AS JSON)" if is_pg else ":config"
    for t in SEED_TENANTS:
        if bind.execute(sa_text("SELECT 1 FROM tenants WHERE id = :id"), {"id": t["id"]}).first():
            continue
        bind.execute(
            sa_text(
                "INSERT INTO tenants (id, name, type, region, config, is_active) "
                f"VALUES (:id, :name, :type, :region, {config_ph}, :is_active)"
            ),
            {"id": t["id"], "name": t["name"], "type": t["type"], "region": t["region"],
             "config": json.dumps(t["config"]), "is_active": True},
        )

    # 3. tenant_id column + index on every content table and users
    for table in _CONTENT_TABLES + ["users"]:
        if table not in tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        if "tenant_id" not in cols:
            bind.execute(sa_text(
                f"ALTER TABLE {table} ADD COLUMN tenant_id VARCHAR(40) NOT NULL DEFAULT 'internal'"
            ))
        bind.execute(sa_text(f"CREATE INDEX IF NOT EXISTS ix_{table}_tenant_id ON {table} (tenant_id)"))

    # 4. RLS policies (Postgres only)
    if is_pg:
        for table in _CONTENT_TABLES:
            if table not in tables and table not in set(sa_inspect(bind).get_table_names()):
                continue
            bind.execute(sa_text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            bind.execute(sa_text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
            bind.execute(sa_text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
            bind.execute(sa_text(
                f"CREATE POLICY tenant_isolation ON {table} "
                f"USING ({_RLS_PREDICATE}) WITH CHECK ({_RLS_PREDICATE})"
            ))


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())

    if is_pg:
        for table in _CONTENT_TABLES:
            if table in tables:
                bind.execute(sa_text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
                bind.execute(sa_text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
                bind.execute(sa_text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))

    for table in _CONTENT_TABLES + ["users"]:
        if table not in tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        if "tenant_id" in cols:
            op.drop_column(table, "tenant_id")

    if "tenants" in tables:
        op.drop_table("tenants")
