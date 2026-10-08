"""Per-tenant, per-UTC-day Apollo credit budget for the Recruiter Contact Finder.

A sibling of app/services/apollo_budget.py (the account-wide harvest cap), but keyed
on (usage_date, tenant_id) so every workspace gets its OWN daily pool
(settings.recruiter_finder_daily_cap, default 50) that can't be starved by another
tenant's lookups or by harvest runs. The Contact Finder service reserves a credit
here BEFORE each credit-spending Apollo reveal; when the day's budget is spent,
``try_consume`` returns False and the worker marks the remaining rows ``queued``.

Atomicity: a single conditional ``UPDATE ... WHERE count + cost <= cap`` whose
row-count is the gate — row-locked in Postgres, globally serialized in SQLite.

Best-effort: a DB error fails **open** (allows the call, logs a warning), matching
apollo_budget. ``cap <= 0`` = unlimited (counted, never denied).

The global apollo_daily_cap still applies at the ApolloClient chokepoint as the
account-wide backstop; this gate is the feature's user-facing, per-tenant limit.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.dependencies import get_session_factory

logger = structlog.get_logger(__name__)


def _utc_today():
    return datetime.now(timezone.utc).date()


async def _reserve(db: AsyncSession, today, tenant_id: str, cap: int, cost: int) -> bool:
    """Atomic conditional increment of today's (tenant) counter row (no commit — the
    caller owns the transaction). Returns True when the bump was allowed. ``cap <= 0``
    = unlimited (counted, never denied)."""
    await db.execute(
        sa_text(
            "INSERT INTO recruiter_finder_usage (usage_date, tenant_id, count) "
            "VALUES (:d, :t, 0) ON CONFLICT (usage_date, tenant_id) DO NOTHING"
        ),
        {"d": today, "t": tenant_id},
    )
    if cap and cap > 0:
        result = await db.execute(
            sa_text(
                "UPDATE recruiter_finder_usage "
                "SET count = count + :cost, updated_at = CURRENT_TIMESTAMP "
                "WHERE usage_date = :d AND tenant_id = :t AND count + :cost <= :cap"
            ),
            {"cost": cost, "d": today, "t": tenant_id, "cap": cap},
        )
        return (result.rowcount or 0) > 0
    # Unlimited — count for observability but never deny.
    await db.execute(
        sa_text(
            "UPDATE recruiter_finder_usage "
            "SET count = count + :cost, updated_at = CURRENT_TIMESTAMP "
            "WHERE usage_date = :d AND tenant_id = :t"
        ),
        {"cost": cost, "d": today, "t": tenant_id},
    )
    return True


def _resolve_cap(cap: int | None) -> int:
    return get_settings().recruiter_finder_daily_cap if cap is None else cap


async def try_consume(cost: int, tenant_id: str, *, cap: int | None = None) -> bool:
    """Atomically reserve ``cost`` Apollo credits for ``tenant_id`` today, if its
    daily cap allows. True when reserved (caller may spend), False when the cap is
    reached (caller must skip/queue). ``cap`` defaults to
    settings.recruiter_finder_daily_cap; ``cap <= 0`` = unlimited. Fail-open on any
    DB error."""
    settings = get_settings()
    cap = _resolve_cap(cap)
    today = _utc_today()
    try:
        factory = get_session_factory(settings)
        async with factory() as db:
            allowed = await _reserve(db, today, tenant_id, cap, cost)
            await db.commit()
            if not allowed:
                logger.info("recruiter_finder_cap_reached", tenant_id=tenant_id, cap=cap, date=str(today))
            return allowed
    except Exception as exc:
        logger.warning("recruiter_finder_budget_check_failed", tenant_id=tenant_id, error=str(exc))
        return True  # fail-open: don't block enrichment on a DB blip


async def remaining_today(tenant_id: str, *, cap: int | None = None) -> int | None:
    """Credits left for ``tenant_id`` under today's cap (clamped ≥ 0), or None when
    unlimited (``cap <= 0``). None on any DB error (treat as "go ahead")."""
    settings = get_settings()
    cap = _resolve_cap(cap)
    if not cap or cap <= 0:
        return None
    today = _utc_today()
    try:
        factory = get_session_factory(settings)
        async with factory() as db:
            row = await db.execute(
                sa_text("SELECT count FROM recruiter_finder_usage WHERE usage_date = :d AND tenant_id = :t"),
                {"d": today, "t": tenant_id},
            )
            return max(0, cap - int(row.scalar() or 0))
    except Exception as exc:
        logger.warning("recruiter_finder_budget_remaining_failed", tenant_id=tenant_id, error=str(exc))
        return None


async def usage_today(tenant_id: str) -> int:
    """Credits already spent by ``tenant_id`` today (0 on error/none)."""
    settings = get_settings()
    today = _utc_today()
    try:
        factory = get_session_factory(settings)
        async with factory() as db:
            row = await db.execute(
                sa_text("SELECT count FROM recruiter_finder_usage WHERE usage_date = :d AND tenant_id = :t"),
                {"d": today, "t": tenant_id},
            )
            return int(row.scalar() or 0)
    except Exception as exc:
        logger.warning("recruiter_finder_usage_today_failed", tenant_id=tenant_id, error=str(exc))
        return 0


async def usage_history(tenant_id: str, days: int = 14) -> list[dict]:
    """Zero-filled daily spend for ``tenant_id`` over the last ``days`` UTC days
    (oldest first), for the usage chart: ``[{"date": "YYYY-MM-DD", "count": int}, …]``."""
    settings = get_settings()
    today = _utc_today()
    start = today - timedelta(days=days - 1)
    counts: dict[str, int] = {}
    try:
        factory = get_session_factory(settings)
        async with factory() as db:
            rows = await db.execute(
                sa_text(
                    "SELECT usage_date, count FROM recruiter_finder_usage "
                    "WHERE tenant_id = :t AND usage_date >= :start"
                ),
                {"t": tenant_id, "start": start},
            )
            for usage_date, count in rows.all():
                counts[str(usage_date)] = int(count or 0)
    except Exception as exc:
        logger.warning("recruiter_finder_usage_history_failed", tenant_id=tenant_id, error=str(exc))
    out: list[dict] = []
    for i in range(days):
        d = start + timedelta(days=i)
        out.append({"date": str(d), "count": counts.get(str(d), 0)})
    return out
