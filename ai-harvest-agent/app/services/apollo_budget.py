"""Global per-UTC-day Apollo credit-call budget — the hard daily cap backstop.

Apollo credits are account-wide (one API key), a harvest runs in a detached task /
proactor thread, and scheduled runs are separate triggers, so an in-memory counter is
useless — the cap MUST be DB-backed to hold across runs/processes. This module owns
the `apollo_daily_usage` counter (one row per UTC date) and exposes a single atomic
``try_consume`` that ApolloClient calls BEFORE every credit-spending request. When the
day's budget is exhausted, ``try_consume`` returns False and the client short-circuits
to its empty/no-match result (never raises).

Atomicity: the gate is a single conditional ``UPDATE ... WHERE count + cost <= cap``
whose row-count tells us whether the bump was allowed — row-locked in Postgres and
globally serialized in SQLite, so two concurrent callers can't both pass at the limit.

Best-effort: a DB error fails **open** (allows the call, logs a warning), consistent
with the app's other best-effort DB helpers (db_read/db_write) — the per-run caps still
bound usage, and a DB outage means little is running anyway. ``cap <= 0`` = unlimited.
"""
from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.dependencies import get_session_factory

logger = structlog.get_logger(__name__)


def _utc_today():
    return datetime.now(timezone.utc).date()


async def _reserve(db: AsyncSession, today, cap: int, cost: int) -> bool:
    """Atomic conditional increment against today's counter row (no commit — the
    caller owns the transaction). Returns True when the bump was allowed. ``cap <= 0``
    = unlimited (counted, never denied). Split out so it's testable against any session.
    The single conditional UPDATE's row-count is the gate: row-locked in Postgres,
    globally serialized in SQLite, so two concurrent callers can't both pass at the cap."""
    await db.execute(
        sa_text(
            "INSERT INTO apollo_daily_usage (usage_date, count) "
            "VALUES (:d, 0) ON CONFLICT (usage_date) DO NOTHING"
        ),
        {"d": today},
    )
    if cap and cap > 0:
        result = await db.execute(
            sa_text(
                "UPDATE apollo_daily_usage "
                "SET count = count + :cost, updated_at = CURRENT_TIMESTAMP "
                "WHERE usage_date = :d AND count + :cost <= :cap"
            ),
            {"cost": cost, "d": today, "cap": cap},
        )
        return (result.rowcount or 0) > 0
    # Unlimited — count for observability but never deny.
    await db.execute(
        sa_text(
            "UPDATE apollo_daily_usage "
            "SET count = count + :cost, updated_at = CURRENT_TIMESTAMP "
            "WHERE usage_date = :d"
        ),
        {"cost": cost, "d": today},
    )
    return True


async def _remaining(db: AsyncSession, today, cap: int) -> int | None:
    """Credits left under ``cap`` for ``today`` (clamped ≥ 0), None when unlimited."""
    if not cap or cap <= 0:
        return None
    row = await db.execute(
        sa_text("SELECT count FROM apollo_daily_usage WHERE usage_date = :d"),
        {"d": today},
    )
    return max(0, cap - int(row.scalar() or 0))


async def try_consume(cost: int = 1, *, cap: int | None = None) -> bool:
    """Atomically reserve ``cost`` Apollo credit-calls for today, if the daily cap
    allows. Returns True when reserved (caller may spend), False when the cap is
    reached (caller must skip). ``cap`` defaults to settings.apollo_daily_cap; ``cap
    <= 0`` means unlimited (always allowed, still counted). Fail-open on any DB error.
    """
    settings = get_settings()
    cap = settings.apollo_daily_cap if cap is None else cap
    today = _utc_today()
    try:
        factory = get_session_factory(settings)
        async with factory() as db:
            allowed = await _reserve(db, today, cap, cost)
            await db.commit()
            if not allowed:
                logger.info("apollo_daily_cap_reached", cap=cap, date=str(today))
            return allowed
    except Exception as exc:
        logger.warning("apollo_budget_check_failed", error=str(exc))
        return True  # fail-open: don't block enrichment on a DB blip


async def remaining_today(*, cap: int | None = None) -> int | None:
    """Credits left under today's cap: ``cap - count`` (clamped ≥ 0), or None when
    unlimited (``cap <= 0``). None on any DB error so callers treat it as "go ahead"
    and let ``try_consume`` be the real gate. Used for pre-flight skips / logging."""
    settings = get_settings()
    cap = settings.apollo_daily_cap if cap is None else cap
    if not cap or cap <= 0:
        return None
    today = _utc_today()
    try:
        factory = get_session_factory(settings)
        async with factory() as db:
            return await _remaining(db, today, cap)
    except Exception as exc:
        logger.warning("apollo_budget_remaining_failed", error=str(exc))
        return None
