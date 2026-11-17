"""Auth domain models: User + OTPVerification ORM tables and their Pydantic schemas."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.validators import validate_company_email
from app.models.harvest import Base


class OTPPurpose:
    LOGIN = "login"


USER_ROLE = "user"
ADMIN_ROLE = "admin"
BUSINESS_LEAD_ROLE = "business_lead"
USER_ROLES = (USER_ROLE, ADMIN_ROLE, BUSINESS_LEAD_ROLE)

# The roles every database must hold (the `roles` table is the source of truth for
# what may be assigned; these rows are upserted on boot by main._ensure_roles and by
# alembic 0036). Keys are stored lower-case in users.role (FK → roles.name).
SEED_ROLES: tuple[dict[str, str], ...] = (
    {"name": ADMIN_ROLE, "label": "Admin",
     "description": "Full access: every phone log, role management and admin tools."},
    {"name": BUSINESS_LEAD_ROLE, "label": "Business Lead",
     "description": "Calls recruiters and sees the phone logs of the calls they own."},
    {"name": USER_ROLE, "label": "User",
     "description": "Standard access to harvested jobs and outreach."},
)


# ── ORM Models ───────────────────────────────────────────────────────────────────

class RoleORM(Base):
    """An assignable user role. Global (not tenant-scoped): the same set of roles
    exists in every workspace. users.role references `name`."""

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    label: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserORM(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, server_default="'internal'", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # roles.name — "user" | "admin" | "business_lead" (SEED_ROLES). Admins read every
    # call log and manage roles (app/core/dependencies.require_admin); business leads
    # read the call logs they own (require_phone_logs_access).
    role: Mapped[str] = mapped_column(
        String(20), ForeignKey("roles.name", onupdate="CASCADE"),
        nullable=False, default="user", server_default="user",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserSessionORM(Base):
    """Server-side persistent session created after OTP verification.

    The raw session token lives only in the client's HttpOnly cookie; we store
    just its SHA-256 hash, so a DB leak never exposes usable tokens. `expires_at`
    slides forward on use (see SessionService) to keep daily users signed in."""

    __tablename__ = "user_sessions"
    __table_args__ = (
        Index("ix_user_sessions_user_id", "user_id"),
        Index("ix_user_sessions_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    user_agent: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OTPVerificationORM(Base):
    __tablename__ = "otp_verifications"
    __table_args__ = (
        Index("ix_otp_verifications_email_purpose", "email", "purpose"),
        Index("ix_otp_verifications_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    otp_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(String(30), nullable=False, default=OTPPurpose.LOGIN)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── Pydantic Schemas ─────────────────────────────────────────────────────────────

class RequestOTPIn(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        """Accept only allowed workspace-domain emails; returns the lowercased address."""
        return validate_company_email(v)


class VerifyOTPIn(BaseModel):
    email: str
    otp: str
    # Login-page workspace switch ("internal" | "us" | "in"). FOR NOW this decides
    # the user's tenant — any allowed user may enter any workspace; omitted/None
    # falls back to the email-domain map (see AuthService._get_or_create_user).
    workspace: str | None = None

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        """Accept only allowed workspace-domain emails; returns the lowercased address."""
        return validate_company_email(v)

    @field_validator("otp")
    @classmethod
    def _validate_otp_format(cls, v: str) -> str:
        """Reject an OTP containing anything but digits (length is not checked here)."""
        if not v.isdigit():
            raise ValueError("OTP must be numeric")
        return v

    @field_validator("workspace")
    @classmethod
    def _validate_workspace(cls, v: str | None) -> str | None:
        """Allow None or a known login-page workspace key; raise ValueError otherwise."""
        from app.models.tenant import WORKSPACE_TENANTS  # avoid import cycle

        if v is not None and v not in WORKSPACE_TENANTS:
            raise ValueError("Unknown workspace")
        return v


class MessageResponse(BaseModel):
    message: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AuthenticatedUser(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    is_active: bool
    is_verified: bool
    tenant_id: str = "internal"
    role: str = USER_ROLE

    @field_validator("role", mode="before")
    @classmethod
    def _normalize_role(cls, v: object) -> str:
        """Lower-case and trim the stored role ("Admin", "ADMIN ", NULL → "admin"/"user").

        Roles are sometimes edited by hand in the users table; without this a value
        like "Admin" silently counted as a normal user both here (is_admin) and in
        the UI, which compares /auth/me's role to "admin" exactly.
        """
        return str(v or USER_ROLE).strip().lower() or USER_ROLE

    @property
    def is_admin(self) -> bool:
        """True when the user's role is the admin role."""
        return self.role == ADMIN_ROLE

    @property
    def is_business_lead(self) -> bool:
        """True when the user's role is the Business Lead role."""
        return self.role == BUSINESS_LEAD_ROLE


class RoleOut(BaseModel):
    """GET /auth/roles item — what the User roles page's dropdown shows."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    label: str
    description: str
