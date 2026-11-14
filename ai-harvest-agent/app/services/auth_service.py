"""Business logic for OTP request/verification and access-token issuance."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.security import SlidingWindowCounter, generate_otp, hash_otp, verify_otp_hash
from app.models.auth import OTPPurpose, OTPVerificationORM, UserORM
from app.models.tenant import INTERNAL_TENANT_ID, tenant_for_email
from app.services.email_service import EmailSender

logger = structlog.get_logger(__name__)


def _aware_utc(dt: datetime) -> datetime:
    """Treat a naive datetime as UTC so it can be compared with ``now(timezone.utc)``."""
    # SQLite has no real timezone-aware storage — datetimes read back through
    # it come back naive even though we always wrote UTC. Normalize so
    # comparisons/arithmetic against datetime.now(timezone.utc) never raise.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class OTPRequestError(Exception):
    """Raised when an OTP cannot be issued right now (e.g. resend cooldown)."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        """``retry_after`` is the seconds until another OTP may be requested, if known."""
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


class OTPVerifyError(Exception):
    """Raised for any OTP verification failure — the route maps this to a
    single generic 401 so invalid/expired/consumed/too-many-attempts are all
    indistinguishable to the caller."""


class OTPVerifyThrottledError(OTPVerifyError):
    """Raised when the client IP has too many failed verifications in the window —
    the route maps this to 429 with ``retry_after``. The OTP is not even checked."""

    def __init__(self, message: str, retry_after: int) -> None:
        """``retry_after`` is the seconds until this IP may try again."""
        super().__init__(message)
        self.retry_after = retry_after


# ── Per-client-IP OTP throttles (in-process memory) ─────────────────────────────
# Complements the per-email limits (resend cooldown, per-OTP attempt cap): those
# stop hammering one address, these stop one IP spraying many addresses. Keyed by
# the real client IP (app.core.security.client_ip), passed in by the route.
_ip_counters: dict[str, SlidingWindowCounter] = {}


def _ip_counter(name: str, window_seconds: int) -> SlidingWindowCounter:
    """The shared counter ``name``, rebuilt if the configured window changed."""
    counter = _ip_counters.get(name)
    if counter is None or counter.window != float(window_seconds):
        counter = _ip_counters[name] = SlidingWindowCounter(window_seconds)
    return counter


def reset_otp_ip_throttles() -> None:
    """Forget all per-IP OTP throttle state (tests)."""
    _ip_counters.clear()


class AuthService:
    def __init__(self, db: AsyncSession, settings: Settings, email_sender: EmailSender) -> None:
        """Bind to the request's DB session; this service flushes but never commits."""
        self._db = db
        self._settings = settings
        self._email_sender = email_sender

    # ── Request OTP ──────────────────────────────────────────────────────────────

    async def request_otp(
        self, email: str, purpose: str = OTPPurpose.LOGIN, client_ip: str | None = None,
    ) -> None:
        """Issue a fresh OTP for ``email`` and send it by email.

        Raises OTPRequestError (with ``retry_after``) when ``client_ip`` has made more
        than OTP_REQUEST_IP_MAX requests in OTP_IP_WINDOW_SECONDS (every request counts),
        or inside the per-email resend cooldown. Every earlier unconsumed OTP for the
        email/purpose is consumed so only the newest can verify. Only the HMAC of the
        code is stored; the plain code goes to the sender.
        """
        settings = self._settings
        now = datetime.now(timezone.utc)

        if client_ip and settings.otp_request_ip_max > 0:
            counter = _ip_counter("request", settings.otp_ip_window_seconds)
            if counter.add(client_ip) > settings.otp_request_ip_max:
                logger.warning("otp_request_ip_throttled", client_ip=client_ip)
                raise OTPRequestError(
                    "Too many OTP requests. Please try again later.",
                    retry_after=counter.retry_after(client_ip),
                )

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

    async def verify_otp(
        self,
        email: str,
        otp: str,
        purpose: str = OTPPurpose.LOGIN,
        workspace: str | None = None,
        client_ip: str | None = None,
    ) -> UserORM:
        """Check ``otp`` against the latest OTP for ``email`` and return the (new) user.

        Raises a generic OTPVerifyError when the OTP is missing, consumed, expired,
        out of attempts or wrong; a wrong code bumps ``attempt_count`` uncommitted (the
        route must commit it). Every such failure also counts against ``client_ip``;
        once it reaches OTP_VERIFY_IP_MAX_FAILURES within OTP_IP_WINDOW_SECONDS, further
        verifies from that IP raise OTPVerifyThrottledError without checking the code.
        Consumption is an atomic conditional UPDATE so only one concurrent verify wins.
        ``workspace`` picks the user's tenant (see _get_or_create_user). Issuing the
        token/session cookie is left to the route.
        """
        settings = self._settings
        failures = None
        if client_ip and settings.otp_verify_ip_max_failures > 0:
            failures = _ip_counter("verify_fail", settings.otp_ip_window_seconds)
            if failures.count(client_ip) >= settings.otp_verify_ip_max_failures:
                logger.warning("otp_verify_ip_throttled", client_ip=client_ip)
                raise OTPVerifyThrottledError(
                    "Too many failed attempts. Please try again later.",
                    retry_after=failures.retry_after(client_ip),
                )
        try:
            return await self._verify_otp(email, otp, purpose, workspace)
        except OTPVerifyError:
            if failures is not None:
                failures.add(client_ip)
            raise

    async def _verify_otp(
        self, email: str, otp: str, purpose: str, workspace: str | None,
    ) -> UserORM:
        """verify_otp's body (see there) without the per-IP failure throttle."""
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

        user = await self._get_or_create_user(email, workspace=workspace)
        logger.info("otp_verified", email=email, purpose=purpose, user_id=user.id,
                    tenant=user.tenant_id)
        # Return the user; the route mints the access token and persistent session
        # (it owns the Response needed to set the session cookie).
        return user

    # ── Internals ────────────────────────────────────────────────────────────────

    async def _latest_otp(self, email: str, purpose: str) -> OTPVerificationORM | None:
        """Most recently created OTP row for the email/purpose, consumed or not."""
        result = await self._db.execute(
            select(OTPVerificationORM)
            .where(OTPVerificationORM.email == email, OTPVerificationORM.purpose == purpose)
            .order_by(OTPVerificationORM.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _get_or_create_user(self, email: str, workspace: str | None = None) -> UserORM:
        """Load or create the verified user and (re)assign its tenant for this login.

        Tenant = the chosen workspace, else the email-domain mapping, else internal. An
        existing user's tenant is overwritten on every login. Flushes, no commit.
        """
        result = await self._db.execute(select(UserORM).where(UserORM.email == email))
        user = result.scalar_one_or_none()
        # FOR NOW: the login page's workspace switch decides the tenant — any
        # allowed user may enter any workspace (each login can pick a different
        # one). The email-domain map (northwindtalent -> client_us, …) is only
        # the fallback when no workspace was sent (API clients / old frontends).
        # To re-tighten per-domain assignment later, ignore `workspace` here.
        from app.models.tenant import WORKSPACE_TENANTS

        tenant_id = (
            WORKSPACE_TENANTS.get(workspace)
            or tenant_for_email(email)
            or INTERNAL_TENANT_ID
        )
        if user is None:
            user = UserORM(email=email, is_active=True, is_verified=True, tenant_id=tenant_id)
            self._db.add(user)
        else:
            user.is_verified = True
            if user.tenant_id != tenant_id:
                user.tenant_id = tenant_id
        await self._db.flush()
        return user
