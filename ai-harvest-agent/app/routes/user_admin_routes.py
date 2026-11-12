"""
User roles (admin-only "User roles" page).

Endpoints
─────────
GET    /auth/users               — users visible to the admin's tenant, admins first
PATCH  /auth/users/{id}/role     — set a user's role ("user" | "admin")

Mounted under /auth so nginx's existing `auth` proxy rule covers it. Users only
exist after their first OTP login, so the list is everyone who has signed in.
Scoped with apply_tenant (internal admins see every tenant's users). An admin
cannot change their own role, so a tenant can't lose its last admin by accident.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db_session, require_admin
from app.core.tenant_context import apply_tenant
from app.models.auth import ADMIN_ROLE, AuthenticatedUser, UserORM

router = APIRouter(tags=["Auth"])


class SetRoleRequest(BaseModel):
    role: Literal["user", "admin"]


def _user_view(u: UserORM) -> dict[str, Any]:
    return {
        "id": u.id,
        "email": u.email,
        "tenant_id": u.tenant_id,
        "role": u.role,
        "is_active": u.is_active,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


@router.get("/auth/users", summary="List users and their roles (admins only)")
async def list_users(
    current_user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    stmt = apply_tenant(
        select(UserORM).order_by(case((UserORM.role == ADMIN_ROLE, 0), else_=1), UserORM.email),
        UserORM.tenant_id,
    )
    return {"items": [_user_view(u) for u in (await db.execute(stmt)).scalars().all()]}


@router.patch("/auth/users/{user_id}/role", summary="Set a user's role (admins only)")
async def set_user_role(
    user_id: str,
    body: SetRoleRequest,
    current_user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    user = (await db.execute(apply_tenant(
        select(UserORM).where(UserORM.id == user_id), UserORM.tenant_id,
    ))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.id == current_user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="You can't change your own role.")
    user.role = body.role
    await db.commit()
    return _user_view(user)
