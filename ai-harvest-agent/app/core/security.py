"""API Key authentication dependency, plus OTP hashing / JWT primitives for
the email-login flow. Kept in one module since both are "security helpers"
for the app, following the existing file-per-concern layout under app/core/.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from jose import JWTError, jwt

from app.config import Settings, get_settings

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    api_key: str | None = Security(API_KEY_HEADER),
) -> str:
    """FastAPI dependency that validates the X-API-Key header."""
    settings = get_settings()
    if api_key is None or api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return api_key


# ── OTP hashing ──────────────────────────────────────────────────────────────────
#
# HMAC-SHA256 (keyed by app_secret_key) rather than bcrypt/passlib: OTPs are
# short 6-digit codes that already expire in minutes and are attempt-limited,
# so a slow password hash buys nothing here, and it sidesteps the well-known
# passlib/bcrypt>=4.1 incompatibility.

def generate_otp(length: int) -> str:
    """Cryptographically secure numeric OTP of the given length."""
    return "".join(secrets.choice(string.digits) for _ in range(length))


def hash_otp(otp: str, settings: Settings) -> str:
    return hmac.new(settings.app_secret_key.encode(), otp.encode(), hashlib.sha256).hexdigest()


def verify_otp_hash(otp: str, otp_hash: str, settings: Settings) -> bool:
    return hmac.compare_digest(hash_otp(otp, settings), otp_hash)


# ── Session tokens (persistent HttpOnly-cookie login) ─────────────────────────────
#
# The session token is a high-entropy opaque secret (256 bits) — no need for a
# slow KDF. We only ever persist its SHA-256, so the raw value (in the cookie)
# is the sole thing that can resolve a session; a DB leak yields only hashes.

def generate_session_token() -> str:
    """Cryptographically secure, URL-safe opaque session token (~43 chars)."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """SHA-256 hex digest — the form stored in user_sessions.token_hash."""
    return hashlib.sha256(token.encode()).hexdigest()


# ── JWT access tokens ────────────────────────────────────────────────────────────

class InvalidTokenError(Exception):
    """Raised when a bearer token fails signature/expiry validation."""


def create_access_token(*, subject: str, email: str, settings: Settings) -> str:
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=settings.access_token_expire_minutes)
    payload: dict[str, Any] = {
        "sub": subject,
        "email": email,
        "iat": now,
        "exp": expires,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str, settings: Settings) -> dict[str, Any]:
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise InvalidTokenError(str(exc)) from exc
