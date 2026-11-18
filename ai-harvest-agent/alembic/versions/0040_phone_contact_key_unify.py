"""One phone key everywhere: trailing 10 digits.

phone_contact_flags.phone_key used to be the number's digits only ("+91 98400 12345"
→ "919840012345") while unique-contact counts used the trailing 10 digits
("9840012345"), so a Naukri 10-digit number and the same LinkedIn +91 number missed
each other. phone_verification_service.normalize_phone now returns
app.core.contact_keys.phone_key, and per-contact call history looks calls up by the
same key. This migration:

  * re-keys existing phone_contact_flags rows to the trailing-10-digit key. Rows that
    collapse onto one (tenant_id, phone_key) — ux_phone_contact_flags_tenant_phone is
    unique — are merged: earliest non-null of each *_at stamp and created_at, a
    non-empty correct_number (newer row first), and last_outcome / last_call_id /
    updated_by_email / updated_at / phone from the row with the newer updated_at; the
    other rows are deleted before the survivor's key changes.
  * PostgreSQL only: indexes phone_call_logs by (tenant_id,
    right(regexp_replace(phone, '\\D', '', 'g'), 10)) for the history lookups.

Mirrored by main._ensure_phone_contact_keys (the app does this at startup).
Idempotent. The merge is not reversible; downgrade only drops the index.

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-18

"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

from alembic import op

# revision identifiers, used by Alembic.
revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

_STAMPS = ("invalid_at", "dnc_at", "warm_at", "reenrich_requested_at")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_INDEX = "ix_phone_call_logs_tenant_phone_key"


def _key(raw: str | None) -> str:
    """app.core.contact_keys.phone_key (copied: migrations stay self-contained)."""
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else digits


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa_inspect(bind).get_table_names())
    if "phone_contact_flags" in tables:
        t = sa.Table("phone_contact_flags", sa.MetaData(), autoload_with=bind)
        groups: dict[tuple[str, str], list] = {}
        for r in bind.execute(sa.select(t)).mappings().all():
            groups.setdefault((r["tenant_id"], _key(r["phone_key"])), []).append(r)
        for (_tenant, key), group in groups.items():
            if not key or (len(group) == 1 and group[0]["phone_key"] == key):
                continue
            order = lambda g: (_aware(g["updated_at"]) or _EPOCH, g["id"])  # noqa: E731
            newest = max(group, key=order)
            keeper = next((g for g in group if g["phone_key"] == key), newest)
            values = {"phone_key": key}
            for col in (*_STAMPS, "created_at"):
                present = [g[col] for g in group if g[col] is not None]
                values[col] = min(present, key=_aware) if present else None
            values["correct_number"] = next(
                (g["correct_number"] for g in sorted(group, key=order, reverse=True)
                 if (g["correct_number"] or "").strip()), "")
            for col in ("last_outcome", "last_call_id", "updated_by_email", "updated_at", "phone"):
                values[col] = newest[col]
            others = [g["id"] for g in group if g["id"] != keeper["id"]]
            if others:
                bind.execute(t.delete().where(t.c.id.in_(others)))
            bind.execute(t.update().where(t.c.id == keeper["id"]).values(**values))
    if bind.dialect.name == "postgresql" and "phone_call_logs" in tables:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {_INDEX} ON phone_call_logs "
            "(tenant_id, right(regexp_replace(phone, '\\D', '', 'g'), 10))"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
