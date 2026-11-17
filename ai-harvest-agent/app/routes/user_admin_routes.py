"""
User roles (admin-only "User roles" page).

Endpoints
─────────
GET    /auth/roles               — every assignable role (the page's dropdown)
GET    /auth/users               — users visible to the admin's tenant, admins first
PATCH  /auth/users/{id}/role     — set a user's role (any roles.name, e.g. "business_lead")

Mounted under /auth so nginx's existing `auth` proxy rule covers it. Users only
exist after their first OTP login, so the list is everyone who has signed in.
Scoped with apply_tenant (internal admins see every tenant's users). An admin
cannot change their own role, so a tenant can't lose its last admin by accident.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db_session, require_admin
from app.core.tenant_context import apply_tenant
from app.models.auth import ADMIN_ROLE, AuthenticatedUser, RoleOut, UserORM
from app.services import role_service

router = APIRouter(tags=["Auth"])


class SetRoleRequest(BaseModel):
    # Validated against the roles table in the handler (422 for an unknown role).
    role: str = Field(..., min_length=1, max_length=20)


def _user_view(u: UserORM, labels: dict[str, str]) -> dict[str, Any]:
    """Serialise a user row for the User roles page (no secrets; ISO `created_at` or None)."""
    return {
        "id": u.id,
        "email": u.email,
        "tenant_id": u.tenant_id,
        "role": u.role,
        "role_label": labels.get(u.role, u.role),
        "is_active": u.is_active,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


@router.get("/auth/roles", summary="List assignable roles (admins only)")
async def list_roles(
    current_user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Every assignable role from the roles table, Admin first.

    Returns `{"items": [{id, name, label, description}]}`. Requires the admin role.
    """
    roles = await role_service.list_roles(db)
    return {"items": [RoleOut.model_validate(r).model_dump() for r in roles]}


@router.get("/auth/users", summary="List users and their roles (admins only)")
async def list_users(
    current_user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """List users who have signed in, admins first then by email.

    Scoped to the caller's tenant (internal admins see every tenant). Requires the admin role.
    Returns `{"items": [...]}` with id, email, tenant_id, role, is_active and created_at.
    """
    stmt = apply_tenant(
        select(UserORM).order_by(case((UserORM.role == ADMIN_ROLE, 0), else_=1), UserORM.email),
        UserORM.tenant_id,
    )
    labels = await role_service.labels(db)
    return {"items": [_user_view(u, labels) for u in (await db.execute(stmt)).scalars().all()]}


@router.patch("/auth/users/{user_id}/role", summary="Set a user's role (admins only)")
async def set_user_role(
    user_id: str,
    body: SetRoleRequest,
    current_user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Set a user's role to any role in the roles table and return the updated user.

    Requires the admin role. 422 for a role that isn't in the roles table; 404 if the user is not
    visible in the caller's tenant scope; 400 if the admin tries to change their own role (prevents
    a tenant losing its last admin by accident).
    """
    role = await role_service.get_role(db, body.role)
    if role is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"Unknown role '{body.role.strip()}'")
    user = (await db.execute(apply_tenant(
        select(UserORM).where(UserORM.id == user_id), UserORM.tenant_id,
    ))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.id == current_user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="You can't change your own role.")
    user.role = role.name
    await db.commit()
    return _user_view(user, await role_service.labels(db))
