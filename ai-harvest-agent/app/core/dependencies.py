"""FastAPI dependency injection helpers."""
from __future__ import annotations

from pathlib import Path
from typing import AsyncGenerator

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.core.security import InvalidTokenError, decode_access_token
from app.models.auth import AuthenticatedUser, UserORM
from app.services.email_service import EmailSender
from app.services.llm_service import LLMService
from app.services.playwright_service import PlaywrightService

logger = structlog.get_logger(__name__)

# ── Database ────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SQLITE_PREFIX = "sqlite+aiosqlite:///"


def _resolve_sqlite_url(url: str) -> str:
    if not url.startswith(_SQLITE_PREFIX):
        return url
    raw_path = url[len(_SQLITE_PREFIX):]
    path = Path(raw_path)
    if path.is_absolute():
        return url
    return _SQLITE_PREFIX + (_PROJECT_ROOT / path).resolve().as_posix()


def _build_engine(settings: Settings):
    # SQLite's async dialect (aiosqlite) uses NullPool and rejects pool_size /
    # max_overflow entirely — those only apply to pooled dialects (Postgres, MySQL).
    if settings.database_url.startswith("sqlite"):
        return create_async_engine(_resolve_sqlite_url(settings.database_url), echo=settings.db_echo)
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        echo=settings.db_echo,
    )


_engine = None
_session_factory = None


def get_engine(settings: Settings = Depends(get_settings)):
    global _engine
    if _engine is None:
        _engine = _build_engine(settings)
    return _engine


def get_session_factory(settings: Settings = Depends(get_settings)):
    global _session_factory
    if _session_factory is None:
        engine = _build_engine(settings)
        _session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return _session_factory


async def get_db_session(
    session_factory=Depends(get_session_factory),
) -> AsyncGenerator[AsyncSession, None]:
    """Yield an async DB session, rolling back on error."""
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── LLM Service ──────────────────────────────────────────────────────────────────


def get_llm_service(settings: Settings = Depends(get_settings)) -> LLMService:
    return LLMService(settings)


# ── Playwright ───────────────────────────────────────────────────────────────────


def get_playwright_service(request: Request) -> PlaywrightService:
    """Return the shared Playwright service from app state."""
    service: PlaywrightService = request.app.state.playwright
    return service


# ── Email (OTP delivery) ─────────────────────────────────────────────────────────


def get_email_sender(settings: Settings = Depends(get_settings)) -> EmailSender:
    return EmailSender(settings)


# ── Current user (OTP/JWT email login) ───────────────────────────────────────────

_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedUser:
    """Validate the bearer JWT and return the authenticated user.

    Use as ``current_user: AuthenticatedUser = Depends(get_current_user)`` on
    any route that requires a logged-in user.
    """
    # Dev bypass: when login enforcement is off, every protected route (and
    # /auth/me) resolves to a synthetic user without needing a token.
    if not settings.auth_enabled:
        return AuthenticatedUser(
            id="dev",
            email=f"dev@{settings.allowed_email_domain}",
            is_active=True,
            is_verified=True,
        )

    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized

    try:
        payload = decode_access_token(credentials.credentials, settings)
    except InvalidTokenError as exc:
        unauthorized.detail = "Invalid or expired token"
        raise unauthorized from exc

    result = await db.execute(select(UserORM).where(UserORM.id == payload.get("sub")))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        unauthorized.detail = "User not found or inactive"
        raise unauthorized

    return AuthenticatedUser.model_validate(user)
