"""Apollo ACCOUNT credit balance for the Recruiter Contact Finder page.

``apollo_budget`` / ``recruiter_finder_budget`` are this app's own daily caps (the spend
gate). This module answers a different question: how many credits does the Apollo
account itself still have this billing cycle? When that hits zero every lookup/reveal
fails upstream, so the Finder page locks its actions and shows only history.

Source order (first that yields a reading wins):
  1. ``live``        — ``apollo_credit_usage.fetch_credit_usage()`` (POST
                       /usage_stats/credit_usage_stats; costs 0 credits). Cached in-process
                       for ``_CACHE_TTL_S`` so page loads / polls don't hammer Apollo.
  2. ``snapshot``    — the latest row in ``apollo_credit_snapshots`` (stored every 15 min
                       by the scheduler).
  3. ``harvest_run`` — the latest completed run's ``harvest_runs.apollo_usage``.
  4. ``unavailable`` — no key / Apollo unreachable / nothing stored.

Fail OPEN: ``exhausted`` is True only when a reading for the CURRENT billing cycle says
``remaining <= 0``. Unknown or stale (previous-cycle) readings never block the page.
Never raises.
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.config import get_settings
from app.core.dependencies import get_session_factory
from app.services import apollo_credit_usage as acu

logger = structlog.get_logger(__name__)

_CACHE_TTL_S = 90.0          # a live reading is reused this long
_MIN_FORCE_INTERVAL_S = 10.0  # a forced refresh still won't call Apollo more often than this
_CYCLE_LENGTH = timedelta(days=31)  # snapshots don't store cycle_end — assume ≤ 31-day cycles

_cache: dict[str, Any] = {"value": None, "at": 0.0}
_lock = asyncio.Lock()

# Apollo's wording when the account has no credits left (best-effort text match on the
# upstream error body; the authoritative signal is the credit reading itself).
_EXHAUSTED_HINTS = (
    "insufficient credit", "not enough credit", "out of credit", "no credits",
    "credits exhausted", "credit limit", "exceeded your credit", "run out of credit",
    "ran out of credit", "no more credit",
)


def reset_cache() -> None:
    """Drop the cached live reading (tests; after a top-up)."""
    _cache["value"] = None
    _cache["at"] = 0.0


def _iso(value: Any) -> str | None:
    """ISO string for a datetime / passthrough for a string / None."""
    if isinstance(value, datetime):
        return acu._utc(value).isoformat()
    return value if isinstance(value, str) and value else None


def _block(
    *, source: str, consumed: Any = None, limit: Any = None, remaining: Any = None,
    cycle_start: Any = None, cycle_end: Any = None, as_of: Any = None, reason: str = "",
) -> dict:
    """Build the response block; derives ``remaining`` from limit − consumed when absent and
    marks a reading from an already-ended cycle as ``stale`` (never ``exhausted``)."""
    now = datetime.now(UTC)
    consumed = int(consumed) if consumed is not None else None
    limit = int(limit) if limit is not None else None
    if remaining is None and consumed is not None and limit is not None:
        remaining = max(0, limit - consumed)
    remaining = int(remaining) if remaining is not None else None

    start_dt = cycle_start if isinstance(cycle_start, datetime) else acu._parse_dt(cycle_start)
    end_dt = cycle_end if isinstance(cycle_end, datetime) else acu._parse_dt(cycle_end)
    start_dt, end_dt = acu._utc(start_dt), acu._utc(end_dt)
    stale = False
    if source != "live":
        if end_dt is not None:
            stale = end_dt <= now
        elif start_dt is not None:
            stale = start_dt + _CYCLE_LENGTH <= now
    exhausted = bool(remaining is not None and remaining <= 0 and not stale)
    return {
        "remaining": remaining, "limit": limit, "consumed": consumed,
        "cycle_start": _iso(start_dt), "cycle_end": _iso(end_dt),
        "as_of": _iso(as_of) or now.isoformat(),
        "source": source, "stale": stale, "exhausted": exhausted,
        **({"reason": reason} if reason else {}),
    }


async def _from_snapshot() -> dict | None:
    """Latest stored ``apollo_credit_snapshots`` reading, or None."""
    from sqlalchemy import select

    from app.models.harvest_run import ApolloCreditSnapshotORM as S

    try:
        factory = get_session_factory(get_settings())
        async with factory() as db:
            row = (await db.execute(
                select(S).order_by(S.taken_at.desc()).limit(1)
            )).scalar_one_or_none()
    except Exception as exc:
        logger.warning("apollo_account_credits_snapshot_failed", error=str(exc))
        return None
    if row is None:
        return None
    return _block(source="snapshot", consumed=row.consumed, limit=row.credit_limit,
                  cycle_start=row.cycle_start, as_of=row.taken_at)


async def _from_harvest_run() -> dict | None:
    """``apollo_usage`` stored on the latest completed harvest run, or None."""
    from app.services.harvest_run_service import HarvestRunService

    try:
        factory = get_session_factory(get_settings())
        async with factory() as db:
            usage = await HarvestRunService(db).get_previous_apollo_usage(None)
    except Exception as exc:
        logger.warning("apollo_account_credits_harvest_run_failed", error=str(exc))
        return None
    if not usage or usage.get("consumed") is None:
        return None
    return _block(source="harvest_run", consumed=usage.get("consumed"),
                  limit=usage.get("limit"), remaining=usage.get("left_over"),
                  cycle_start=usage.get("cycle_start"), cycle_end=usage.get("cycle_end"),
                  as_of=usage.get("taken_at"))


async def _live() -> dict | None:
    """A fresh reading from Apollo (0 credits), or None."""
    try:
        usage = await acu.fetch_credit_usage()
    except Exception as exc:  # fetch_credit_usage never raises, but stay defensive
        logger.warning("apollo_account_credits_live_failed", error=str(exc))
        return None
    if not usage:
        return None
    return _block(source="live", consumed=usage.get("consumed"), limit=usage.get("limit"),
                  remaining=usage.get("left_over"), cycle_start=usage.get("cycle_start"),
                  cycle_end=usage.get("cycle_end"))


async def get_account_credits(*, force: bool = False) -> dict:
    """Apollo account credits left this billing cycle (see module docstring for the shape
    and source order). ``force`` bypasses the cache (still rate-limited to one Apollo call
    per ``_MIN_FORCE_INTERVAL_S``). Never raises."""
    try:
        if not get_settings().apollo_api_key:
            return _block(source="unavailable", reason="not_configured")
        async with _lock:
            age = time.monotonic() - float(_cache["at"])
            cached = _cache["value"]
            ttl = _MIN_FORCE_INTERVAL_S if force else _CACHE_TTL_S
            if cached is not None and age < ttl:
                return dict(cached)
            live = await _live()
            if live is not None:
                _cache["value"], _cache["at"] = live, time.monotonic()
                return dict(live)
        return (await _from_snapshot() or await _from_harvest_run()
                or _block(source="unavailable", reason="apollo_unreachable"))
    except Exception as exc:
        logger.warning("apollo_account_credits_failed", error=str(exc))
        return _block(source="unavailable", reason="error")


def is_credits_exhausted_error(exc: BaseException) -> bool:
    """Heuristic: does this Apollo error look like "the account is out of credits"?
    (HTTP 402, or an error body that says so.)"""
    details = getattr(exc, "details", None) or {}
    if details.get("status") == 402:
        return True
    body = f"{details.get('body', '')} {exc}".lower()
    return any(h in body for h in _EXHAUSTED_HINTS)
