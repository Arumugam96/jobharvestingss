"""Assignable user roles (the `roles` table).

The table is the source of truth for which role keys users.role may hold; it is
seeded from app.models.auth.SEED_ROLES on boot (main._ensure_roles) and by alembic
0036. `ensure_seeded` is the async equivalent used by tests and scripts.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import SEED_ROLES, RoleORM

# Dropdown order: most privileged first, then alphabetical by label.
_ORDER = {r["name"]: i for i, r in enumerate(SEED_ROLES)}


async def list_roles(db: AsyncSession) -> list[RoleORM]:
    """Every assignable role, seed roles first (Admin, Business Lead, User)."""
    rows = (await db.execute(select(RoleORM))).scalars().all()
    return sorted(rows, key=lambda r: (_ORDER.get(r.name, len(_ORDER)), r.label or r.name))


async def get_role(db: AsyncSession, name: str) -> RoleORM | None:
    """The role row for `name` (case-insensitive, trimmed), or None if it isn't assignable."""
    key = (name or "").strip().lower()
    if not key:
        return None
    return (await db.execute(select(RoleORM).where(RoleORM.name == key))).scalar_one_or_none()


async def labels(db: AsyncSession) -> dict[str, str]:
    """{role name: label} for serialising users."""
    return {r.name: r.label or r.name for r in (await db.execute(select(RoleORM))).scalars()}


async def ensure_seeded(db: AsyncSession) -> None:
    """Insert any missing SEED_ROLES row (existing rows are left as they are). Commits."""
    have = set((await db.execute(select(RoleORM.name))).scalars())
    for r in SEED_ROLES:
        if r["name"] not in have:
            db.add(RoleORM(name=r["name"], label=r["label"], description=r["description"]))
    await db.commit()
