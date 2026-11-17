"""Roles table + Business Lead role + phone call-log ownership.

  * roles                         — id, name (unique key stored in users.role), label,
                                    description, created_at, updated_at. Seeded with
                                    admin / business_lead / user.
  * users.role                    — values normalised to a lower-case seeded key (an
                                    unknown value falls back to "user"; existing admins
                                    stay admins), then FK → roles.name ON UPDATE CASCADE.
  * phone_call_logs.assigned_to_user_id
                                  — VARCHAR(36) NULL, FK → users.id ON DELETE SET NULL,
                                    indexed. Owner of the call record; backfilled from
                                    called_by_email (case-insensitive users.email match).

Mirrors the runtime helpers in app/main.py (_ensure_roles,
_ensure_phone_call_logs_owner) — create_all never alters an existing table.
Idempotent (safe alongside create_all). Non-destructive: no user or call row is
removed. Downgrade drops the FK, the owner column and the roles table; users.role
keeps its (string) values.

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-17

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from alembic import op

# revision identifiers, used by Alembic.
revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

_SEED_ROLES = (
    {"name": "admin", "label": "Admin",
     "description": "Full access: every phone log, role management and admin tools."},
    {"name": "business_lead", "label": "Business Lead",
     "description": "Calls recruiters and sees the phone logs of the calls they own."},
    {"name": "user", "label": "User",
     "description": "Standard access to harvested jobs and outreach."},
)
_USERS_ROLE_FK = "fk_users_role_roles"
_OWNER_FK = "fk_phone_call_logs_assigned_to_user_id_users"
_OWNER_IX = "ix_phone_call_logs_assigned_to_user_id"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())
    is_pg = bind.dialect.name == "postgresql"

    if "roles" not in tables:
        op.create_table(
            "roles",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(length=20), nullable=False, unique=True),
            sa.Column("label", sa.String(length=60), nullable=False, server_default=""),
            sa.Column("description", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
    existing = {r[0] for r in bind.execute(sa.text("SELECT name FROM roles"))}
    for r in _SEED_ROLES:
        if r["name"] not in existing:
            bind.execute(sa.text(
                "INSERT INTO roles (name, label, description) VALUES (:name, :label, :description)"
            ), r)

    if "users" in tables:
        bind.execute(sa.text(
            "UPDATE users SET role = lower(trim(role)) WHERE role <> lower(trim(role))"))
        bind.execute(sa.text(
            "UPDATE users SET role = 'user' WHERE role NOT IN (SELECT name FROM roles)"))
        fks = inspector.get_foreign_keys("users")
        if is_pg and not any(fk.get("referred_table") == "roles" for fk in fks):
            op.create_foreign_key(_USERS_ROLE_FK, "users", "roles", ["role"], ["name"],
                                  onupdate="CASCADE")

    if "phone_call_logs" in tables:
        cols = {c["name"] for c in inspector.get_columns("phone_call_logs")}
        if "assigned_to_user_id" not in cols:
            op.add_column("phone_call_logs",
                          sa.Column("assigned_to_user_id", sa.String(length=36), nullable=True))
            if is_pg:
                op.create_foreign_key(_OWNER_FK, "phone_call_logs", "users",
                                      ["assigned_to_user_id"], ["id"], ondelete="SET NULL")
        ixs = {i["name"] for i in inspector.get_indexes("phone_call_logs")}
        if _OWNER_IX not in ixs:
            op.create_index(_OWNER_IX, "phone_call_logs", ["assigned_to_user_id"])
        bind.execute(sa.text(
            "UPDATE phone_call_logs SET assigned_to_user_id = ("
            " SELECT u.id FROM users u"
            " WHERE lower(u.email) = lower(phone_call_logs.called_by_email)"
            " LIMIT 1) "
            "WHERE assigned_to_user_id IS NULL AND called_by_email <> ''"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa_inspect(bind)
    tables = set(inspector.get_table_names())
    is_pg = bind.dialect.name == "postgresql"

    if "phone_call_logs" in tables:
        cols = {c["name"] for c in inspector.get_columns("phone_call_logs")}
        if "assigned_to_user_id" in cols:
            if is_pg:
                for fk in inspector.get_foreign_keys("phone_call_logs"):
                    if fk.get("constrained_columns") == ["assigned_to_user_id"] and fk.get("name"):
                        op.drop_constraint(fk["name"], "phone_call_logs", type_="foreignkey")
            ixs = {i["name"] for i in inspector.get_indexes("phone_call_logs")}
            if _OWNER_IX in ixs:
                op.drop_index(_OWNER_IX, table_name="phone_call_logs")
            op.drop_column("phone_call_logs", "assigned_to_user_id")

    if "users" in tables:
        if is_pg:
            for fk in inspector.get_foreign_keys("users"):
                if fk.get("referred_table") == "roles" and fk.get("name"):
                    op.drop_constraint(fk["name"], "users", type_="foreignkey")
        # Pre-0036 code only knows user/admin; business leads become plain users.
        bind.execute(sa.text("UPDATE users SET role = 'user' WHERE role NOT IN ('user', 'admin')"))

    if "roles" in tables:
        op.drop_table("roles")
