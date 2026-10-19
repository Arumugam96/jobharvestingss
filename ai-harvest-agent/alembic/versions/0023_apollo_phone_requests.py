"""Async Apollo phone discovery for the Contact Finder.

Adds, idempotently (safe whether the app's startup create_all / _ensure_* helpers
already ran):
  * apollo_phone_requests — one row per (phone request, person): the ``ref`` embedded
    in Apollo's webhook URL + the Apollo person id correlate the asynchronous delivery
    back to the recruiter / finder item / finder job / reveal-log row
    (app/models/apollo_phone.py::ApolloPhoneRequestORM). Tenant-scoped.
  * apollo_webhook_events — raw log of every delivery to
    POST /recruiter-finder/apollo-phone-webhook (diagnostics, not tenant-scoped).
  * recruiter_finder_items.phone_status + recruiter_finder_items.apollo_person_id.

Mirrors the runtime create_all + _ensure_recruiter_finder_items_columns in app/main.py.
Portable DDL — works on PostgreSQL and SQLite alike.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-19

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text as sa_text

# revision identifiers, used by Alembic.
revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

_REQUESTS = "apollo_phone_requests"
_EVENTS = "apollo_webhook_events"
_ITEMS = "recruiter_finder_items"


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())

    if _REQUESTS not in tables:
        op.create_table(
            _REQUESTS,
            sa.Column("tenant_id", sa.String(length=40), nullable=False, server_default="internal"),
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("ref", sa.String(length=36), nullable=False),
            sa.Column("apollo_person_id", sa.String(length=60), nullable=True),
            sa.Column("recruiter_id", sa.String(length=36), nullable=True),
            sa.Column("finder_item_id", sa.String(length=36), nullable=True),
            sa.Column("finder_job_id", sa.String(length=36), nullable=True),
            sa.Column("reveal_log_id", sa.String(length=36), nullable=True),
            sa.Column("requested_by", sa.String(length=120), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("phone", sa.String(length=50), nullable=False, server_default=""),
            sa.Column("phone_type", sa.String(length=30), nullable=False, server_default=""),
            sa.Column("phone_status", sa.String(length=20), nullable=False, server_default=""),
            sa.Column("dnc_status", sa.String(length=30), nullable=False, server_default=""),
            sa.Column("credits_charged", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_apollo_phone_requests_tenant_id", _REQUESTS, ["tenant_id"])
        op.create_index("ix_apollo_phone_requests_ref", _REQUESTS, ["ref"])
        op.create_index("ix_apollo_phone_requests_apollo_person_id", _REQUESTS, ["apollo_person_id"])
        op.create_index("ix_apollo_phone_requests_finder_item_id", _REQUESTS, ["finder_item_id"])
        op.create_index("ix_apollo_phone_requests_finder_job_id", _REQUESTS, ["finder_job_id"])
        op.create_index("ix_apollo_phone_requests_status", _REQUESTS, ["status"])
        op.create_index("ix_apollo_phone_requests_ref_person", _REQUESTS, ["ref", "apollo_person_id"])
        op.create_index("ix_apollo_phone_requests_status_requested", _REQUESTS, ["status", "requested_at"])

    if _EVENTS not in tables:
        op.create_table(
            _EVENTS,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("ref", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("remote_addr", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("token_ok", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
            sa.Column("people_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("matched_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("found_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("body", sa.Text(), nullable=False, server_default=""),
        )
        op.create_index("ix_apollo_webhook_events_received_at", _EVENTS, ["received_at"])

    if _ITEMS in tables:
        cols = {c["name"] for c in sa_inspect(bind).get_columns(_ITEMS)}
        if "phone_status" not in cols:
            bind.execute(sa_text(
                f"ALTER TABLE {_ITEMS} ADD COLUMN phone_status VARCHAR(20) NOT NULL DEFAULT ''"
            ))
        if "apollo_person_id" not in cols:
            bind.execute(sa_text(f"ALTER TABLE {_ITEMS} ADD COLUMN apollo_person_id VARCHAR(60)"))


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())

    if _ITEMS in tables:
        cols = {c["name"] for c in sa_inspect(bind).get_columns(_ITEMS)}
        for col in ("apollo_person_id", "phone_status"):
            if col in cols:
                op.drop_column(_ITEMS, col)
    if _EVENTS in tables:
        op.drop_table(_EVENTS)
    if _REQUESTS in tables:
        op.drop_table(_REQUESTS)
