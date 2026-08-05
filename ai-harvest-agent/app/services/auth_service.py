"""Business logic for OTP request/verification and access-token issuance."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.security import create_access_token, generate_otp, hash_otp, verify_otp_hash
from app.models.auth import OTPPurpose, OTPVerificationORM, UserORM
from app.services.email_service import EmailSender

logger = structlog.get_logger(__name__)


def _aware_utc(dt: datetime) -> datetime:
    # SQLite has no real timezone-aware storage — datetimes read back through
    # it come back naive even though we always wrote UTC. Normalize so
    # comparisons/arithmetic against datetime.now(timezone.utc) never raise.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class OTPRequestError(Exception):
    """Raised when an OTP cannot be issued right now (e.g. resend cooldown)."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


class OTPVerifyError(Exception):
    """Raised for any OTP verification failure — the route maps this to a
    single generic 401 so invalid/expired/consumed/too-many-attempts are all
    indistinguishable to the caller."""


class AuthService:
    def __init__(self, db: AsyncSession, settings: Settings, email_sender: EmailSender) -> None:
        self._db = db
        self._settings = settings
        self._email_sender = email_sender

    # ── Request OTP ──────────────────────────────────────────────────────────────

    async def request_otp(self, email: str, purpose: str = OTPPurpose.LOGIN) -> None:
        settings = self._settings
        now = datetime.now(timezone.utc)

        latest = await self._latest_otp(email, purpose)
        if latest is not None:
            cooldown_until = _aware_utc(latest.created_at) + timedelta(
                seconds=settings.otp_resend_cooldown_seconds
            )
            if now < cooldown_until:
                raise OTPRequestError(
                    "Please wait before requesting another OTP.",
                    retry_after=max(1, int((cooldown_until - now).total_seconds())),
                )

        # Only the latest OTP for this email/purpose may ever be accepted.
        await self._db.execute(
            update(OTPVerificationORM)
            .where(
                OTPVerificationORM.email == email,
                OTPVerificationORM.purpose == purpose,
                OTPVerificationORM.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )

        otp = generate_otp(settings.otp_length)
        self._db.add(
            OTPVerificationORM(
                email=email,
                otp_hash=hash_otp(otp, settings),
                purpose=purpose,
                expires_at=now + timedelta(seconds=settings.otp_expiry_seconds),
                attempt_count=0,
                max_attempts=settings.otp_max_attempts,
            )
        )
        await self._db.flush()

        # otp only ever lives in this local variable, for exactly long enough
        # to hand it to SMTP — never logged, never persisted, never returned.
        await self._email_sender.send_otp(email, otp)
        logger.info("otp_requested", email=email, purpose=purpose)

    # ── Verify OTP ───────────────────────────────────────────────────────────────

    async def verify_otp(self, email: str, otp: str, purpose: str = OTPPurpose.LOGIN) -> str:
        settings = self._settings
        now = datetime.now(timezone.utc)

        record = await self._latest_otp(email, purpose)
        if (
            record is None
            or record.consumed_at is not None
            or _aware_utc(record.expires_at) < now
            or record.attempt_count >= record.max_attempts
        ):
            raise OTPVerifyError("Invalid or expired OTP")

        if not verify_otp_hash(otp, record.otp_hash, settings):
            await self._db.execute(
                update(OTPVerificationORM)
                .where(
                    OTPVerificationORM.id == record.id,
                    OTPVerificationORM.consumed_at.is_(None),
                )
                .values(attempt_count=OTPVerificationORM.attempt_count + 1)
            )
            # Deliberately not committed here — the route returns a Response
            # (rather than raising) precisely so get_db_session's normal
            # yield -> commit path persists this increment. Raising here would
            # make the 401 response roll it back via the dependency's
            # exception -> rollback handling, defeating the attempt cap.
            raise OTPVerifyError("Invalid or expired OTP")

        # Atomic conditional consume: under concurrent verification of the same
        # OTP, only one request's UPDATE affects a row (rowcount == 1) — every
        # other concurrent request sees rowcount == 0 and fails.
        result = await self._db.execute(
            update(OTPVerificationORM)
            .where(
                OTPVerificationORM.id == record.id,
                OTPVerificationORM.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )
        if result.rowcount != 1:
            raise OTPVerifyError("Invalid or expired OTP")

        user = await self._get_or_create_user(email)
        logger.info("otp_verified", email=email, purpose=purpose, user_id=user.id)
        return create_access_token(subject=user.id, email=user.email, settings=settings)

    # ── Internals ────────────────────────────────────────────────────────────────

    async def _latest_otp(self, email: str, purpose: str) -> OTPVerificationORM | None:
        result = await self._db.execute(
            select(OTPVerificationORM)
            .where(OTPVerificationORM.email == email, OTPVerificationORM.purpose == purpose)
            .order_by(OTPVerificationORM.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _get_or_create_user(self, email: str) -> UserORM:
        result = await self._db.execute(select(UserORM).where(UserORM.email == email))
        user = result.scalar_one_or_none()
        if user is None:
            user = UserORM(email=email, is_active=True, is_verified=True)
            self._db.add(user)
        else:
            user.is_verified = True
        await self._db.flush()
        return user
