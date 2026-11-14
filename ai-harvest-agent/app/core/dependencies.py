"""FastAPI dependency injection helpers."""
from __future__ import annotations

from typing import AsyncGenerator
import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.core.security import InvalidTokenError, decode_access_token
from app.core.tenant_context import bind_session_tenant, set_current_tenant
from app.models.auth import ADMIN_ROLE, AuthenticatedUser, UserORM
from app.models.tenant import INTERNAL_TENANT_ID, SEED_TENANTS
from app.services.email_service import EmailSender
from app.services.llm_service import LLMService
from app.services.playwright_service import PlaywrightService
from app.services.apollo_client import ApolloClient

logger = structlog.get_logger(__name__)

# Tenant ids the dev bypass may impersonate via the X-Dev-Tenant header
# (internal, client_us, client_in). Static set — no per-request DB hit; anything
# else falls back to internal.
_DEV_TENANT_IDS = {t["id"] for t in SEED_TENANTS}
_engine = None
_session_factory = None


def get_engine(settings: Settings = Depends(get_settings)):
    """Return the process-wide async SQLAlchemy engine, creating it on first call.

    The engine is cached in a module global, so later calls ignore ``settings``. Pool
    sizing comes from the DB_* settings and pre-ping is on to drop dead connections.
    """
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            pool_recycle=settings.db_pool_recycle,
            pool_pre_ping=True,
            echo=settings.db_echo,
        )
    return _engine


def get_session_factory(settings: Settings = Depends(get_settings)):
    """Return the cached ``async_sessionmaker`` bound to the shared engine.

    Sessions use ``expire_on_commit=False`` so ORM objects stay readable after commit.
    """
    global _session_factory
    if _session_factory is None:
        engine = get_engine(settings)
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
    """FastAPI factory: a fresh LLMService per request (not cached)."""
    return LLMService(settings)


# ── Playwright ───────────────────────────────────────────────────────────────────


def get_playwright_service(request: Request) -> PlaywrightService:
    """Return the shared Playwright service from app state."""
    service: PlaywrightService = request.app.state.playwright
    return service


# ── Email (OTP delivery) ─────────────────────────────────────────────────────────


def get_email_sender(settings: Settings = Depends(get_settings)) -> EmailSender:
    """FastAPI factory: a fresh EmailSender (used for OTP login emails)."""
    return EmailSender(settings)


# ── Apollo.io contact-enrichment client ──────────────────────────────────────────


def get_apollo_client(settings: Settings = Depends(get_settings)) -> ApolloClient:
    """FastAPI factory for the Apollo client (agents instantiate it directly;
    this exists for any future route that needs Apollo)."""
    return ApolloClient(settings)


# ── Current user (OTP/JWT email login) ───────────────────────────────────────────

_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedUser:
    """Return the authenticated user from either the persistent session cookie
    or a bearer JWT.

    Use as ``current_user: AuthenticatedUser = Depends(get_current_user)`` on
    any route that requires a logged-in user. The cookie is checked first (it's
    what the browser app uses); the bearer path is kept intact for Swagger and
    programmatic API clients.
    """
    # Dev bypass: when login enforcement is off, every protected route (and
    # /auth/me) resolves to a synthetic user without needing a token.
    if not settings.auth_enabled:
        # X-Dev-Tenant lets developers preview a client workspace (slip, feature
        # gating, data scoping) without OTP. Only read here — with auth on this
        # branch is unreachable, so the header is inert in production by
        # construction. Missing/invalid header keeps the internal (all-access)
        # tenant, today's behavior.
        requested = (request.headers.get("x-dev-tenant") or "").strip()
        dev_tenant = requested if requested in _DEV_TENANT_IDS else INTERNAL_TENANT_ID
        set_current_tenant(dev_tenant)
        await bind_session_tenant(db)
        return AuthenticatedUser(
            id="dev",
            email=f"dev@{settings.allowed_email_domain}",
            is_active=True,
            is_verified=True,
            tenant_id=dev_tenant,
            role=ADMIN_ROLE,
        )

    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # ── 1. Persistent session cookie (the browser app's normal path) ──────────
    cookie_token = request.cookies.get(settings.session_cookie_name)
    if cookie_token:
        from app.services.session_service import SessionService  # avoid import cycle

        session = await SessionService(db, settings).resolve(cookie_token)
        if session is not None:
            result = await db.execute(select(UserORM).where(UserORM.id == session.user_id))
            user = result.scalar_one_or_none()
            if user is not None and user.is_active:
                set_current_tenant(user.tenant_id)
                await bind_session_tenant(db)
                return AuthenticatedUser.model_validate(user)
        # Cookie present but stale/revoked — fall through to the bearer path
        # rather than 401 outright, so a client sending both still works.

    # ── 2. Bearer JWT (Swagger / API clients) ─────────────────────────────────
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

    set_current_tenant(user.tenant_id)
    await bind_session_tenant(db)
    return AuthenticatedUser.model_validate(user)


async def require_admin(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """Like get_current_user, but 403 unless the user's role is admin."""
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admins only")
    return current_user


async def get_current_tenant(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> str:
    """The current request's tenant id (`internal` for the in-house team / dev
    bypass). Content queries scope to it; `internal` is all-access."""
    return current_user.tenant_id
