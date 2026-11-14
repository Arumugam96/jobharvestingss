"""API Key authentication dependency, plus OTP hashing / JWT primitives for
the email-login flow. Kept in one module since both are "security helpers"
for the app, following the existing file-per-concern layout under app/core/.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import string
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request, Security, status
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


# ── Real client IP (behind the nginx reverse proxy) ─────────────────────────────
#
# Every browser request reaches the API through nginx, so request.client.host is
# nginx's address for all users. nginx sets X-Real-IP / X-Forwarded-For; those
# headers are only trusted when the direct peer is a loopback/private address (the
# proxy on the same host or docker network) — a client connecting directly from a
# public address could otherwise spoof any IP.

def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """``value`` as an IP address, or None when it is not one."""
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


# Loopback, RFC 1918, link-local, IPv6 ULA. Explicit on purpose: ipaddress's
# is_private also covers documentation/reserved ranges that are not our proxy.
_TRUSTED_PROXY_NETS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10",
))


def _is_trusted_proxy(ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None) -> bool:
    """True for loopback / private (RFC 1918, ULA, link-local) peers — our own proxy."""
    if ip is None:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _TRUSTED_PROXY_NETS)


def client_ip(request: Request) -> str:
    """The real client IP for rate limiting / throttling.

    When the direct peer is a trusted proxy (loopback/private), use X-Real-IP, else
    the right-most X-Forwarded-For entry that is not itself a trusted proxy (entries
    to its left are client-supplied and spoofable). Otherwise, or when the headers
    carry no valid IP, fall back to request.client.host ("unknown" without a peer).
    """
    peer = request.client.host if request.client else ""
    if not _is_trusted_proxy(_parse_ip(peer)):
        return peer or "unknown"
    real_ip = _parse_ip(request.headers.get("x-real-ip", ""))
    if real_ip is not None:
        return str(real_ip)
    hops = [_parse_ip(h) for h in request.headers.get("x-forwarded-for", "").split(",")]
    hops = [h for h in hops if h is not None]
    for hop in reversed(hops):
        if not _is_trusted_proxy(hop):
            return str(hop)
    if hops:
        return str(hops[0])
    return peer or "unknown"


class SlidingWindowCounter:
    """In-process per-key sliding-window hit counter (not shared across workers).

    Stale keys are evicted on a periodic sweep (at most once per window), so the
    map stays bounded by the keys active within the last window.
    """

    def __init__(self, window_seconds: float) -> None:
        """Count hits per key over the trailing ``window_seconds``."""
        self.window = float(window_seconds)
        self._hits: dict[str, list[float]] = {}
        self._last_sweep = 0.0

    def _live(self, key: str, now: float) -> list[float]:
        """Prune ``key``'s expired hits (dropping the key when none remain) and return the rest."""
        cutoff = now - self.window
        hits = [t for t in self._hits.get(key, ()) if t > cutoff]
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def _sweep(self, now: float) -> None:
        """Evict every key with no hit inside the window (runs at most once per window)."""
        if now - self._last_sweep < self.window:
            return
        self._last_sweep = now
        cutoff = now - self.window
        self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > cutoff}

    def count(self, key: str) -> int:
        """Hits recorded for ``key`` within the window."""
        now = time.monotonic()
        self._sweep(now)
        return len(self._live(key, now))

    def add(self, key: str) -> int:
        """Record one hit for ``key`` and return its count within the window (including it)."""
        now = time.monotonic()
        self._sweep(now)
        hits = self._live(key, now)
        hits.append(now)
        self._hits[key] = hits
        return len(hits)

    def retry_after(self, key: str) -> int:
        """Whole seconds until ``key``'s oldest in-window hit expires (>= 1)."""
        hits = self._hits.get(key)
        if not hits:
            return 1
        return max(1, int(hits[0] + self.window - time.monotonic()) + 1)

    def clear(self) -> None:
        """Forget every key (tests / config reload)."""
        self._hits.clear()


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
    """HMAC-SHA256 hex digest of the OTP, keyed by APP_SECRET_KEY (the stored form)."""
    return hmac.new(settings.app_secret_key.encode(), otp.encode(), hashlib.sha256).hexdigest()


def verify_otp_hash(otp: str, otp_hash: str, settings: Settings) -> bool:
    """Constant-time check that ``otp`` hashes to the stored ``otp_hash``."""
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
    """Sign a bearer JWT carrying ``sub`` (user id) and ``email``.

    Expires after ACCESS_TOKEN_EXPIRE_MINUTES; signed with JWT_SECRET_KEY/JWT_ALGORITHM.
    """
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
    """Verify a bearer JWT and return its claims.

    Raises InvalidTokenError on a bad signature, malformed token or expiry.
    """
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise InvalidTokenError(str(exc)) from exc
