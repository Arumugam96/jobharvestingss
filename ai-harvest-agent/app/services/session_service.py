"""Server-side persistent sessions backing the HttpOnly login cookie.

A session is an opaque token (stored client-side only, in a Secure HttpOnly
cookie) whose SHA-256 hash is persisted in ``user_sessions``. It slides: any
authenticated request within the lifetime window pushes ``expires_at`` forward
(throttled to one write per ``session_renew_interval_minutes``), so daily users
almost never have to re-verify an OTP. Sessions end when they expire, are
revoked (logout), or the user is deactivated.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.security import generate_session_token, hash_session_token
from app.models.auth import UserSessionORM

logger = structlog.get_logger(__name__)


def _aware_utc(dt: datetime) -> datetime:
    # Datetimes read back through SQLite come back naive though we always wrote
    # UTC; normalize so comparisons against datetime.now(timezone.utc) never raise.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class SessionService:
    def __init__(self, db: AsyncSession, settings: Settings) -> None:
        self._db = db
        self._settings = settings

    async def create_session(self, user_id: str, user_agent: str = "") -> str:
        """Mint a new session for a freshly authenticated user and return the raw
        token to place in the cookie (only the hash is persisted)."""
        raw = generate_session_token()
        now = datetime.now(timezone.utc)
        self._db.add(
            UserSessionORM(
                user_id=user_id,
                token_hash=hash_session_token(raw),
                user_agent=(user_agent or "")[:255],
                last_seen_at=now,
                expires_at=now + timedelta(days=self._settings.session_lifetime_days),
            )
        )
        await self._db.flush()
        logger.info("session_created", user_id=user_id)
        return raw

    async def resolve(self, raw_token: str) -> UserSessionORM | None:
        """Return the live session for a cookie token, or None if it is missing,
        revoked, or expired. Applies throttled sliding renewal on a hit."""
        if not raw_token:
            return None
        result = await self._db.execute(
            select(UserSessionORM).where(
                UserSessionORM.token_hash == hash_session_token(raw_token),
                UserSessionORM.revoked_at.is_(None),
            )
        )
        session = result.scalar_one_or_none()
        if session is None:
            return None

        now = datetime.now(timezone.utc)
        if _aware_utc(session.expires_at) < now:
            return None

        # Sliding renewal, throttled so we don't write on every request.
        renew_after = timedelta(minutes=self._settings.session_renew_interval_minutes)
        if now - _aware_utc(session.last_seen_at) > renew_after:
            session.last_seen_at = now
            session.expires_at = now + timedelta(days=self._settings.session_lifetime_days)
            await self._db.flush()
        return session

    async def revoke(self, raw_token: str) -> None:
        """Revoke the session identified by a cookie token (idempotent)."""
        if not raw_token:
            return
        now = datetime.now(timezone.utc)
        await self._db.execute(
            update(UserSessionORM)
            .where(
                UserSessionORM.token_hash == hash_session_token(raw_token),
                UserSessionORM.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        logger.info("session_revoked")
