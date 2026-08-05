"""Auth domain models: User + OTPVerification ORM tables and their Pydantic schemas."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import Boolean, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.validators import validate_company_email
from app.models.harvest import Base  # shared metadata — one Base.metadata.create_all() for all tables


class OTPPurpose:
    LOGIN = "login"


# ── ORM Models ───────────────────────────────────────────────────────────────────

class UserORM(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


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
        return validate_company_email(v)


class VerifyOTPIn(BaseModel):
    email: str
    otp: str

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        return validate_company_email(v)

    @field_validator("otp")
    @classmethod
    def _validate_otp_format(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("OTP must be numeric")
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
