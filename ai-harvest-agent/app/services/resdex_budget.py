"""Account-wide per-UTC-day Resdex budget — searches and candidates kept.

Same atomic pattern as app/services/apollo_budget.py (one row per UTC date; a
single conditional ``UPDATE ... WHERE <col> + cost <= cap`` whose row-count is
the gate), with two counters on one row: ``searches`` (one per Resdex search
submitted, every relaxation tier counts) and ``candidates`` (profiles kept).

Unlike Apollo this fails CLOSED: on any DB error no search runs and no candidate
is kept — the counters protect a real recruiter account, so "unsure" means "stop".
``cap <= 0`` = unlimited (still counted).
"""
from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.dependencies import get_session_factory

logger = structlog.get_logger(__name__)

_COLUMNS = ("searches", "candidates")


def _utc_today():
    return datetime.now(timezone.utc).date()


async def _reserve(db: AsyncSession, today, column: str, cap: int, cost: int) -> bool:
    """Atomic conditional bump of ``column`` (no commit — caller owns the
    transaction). True when allowed."""
    if column not in _COLUMNS:
        raise ValueError(f"unknown resdex budget column {column!r}")
    await db.execute(
        sa_text(
            "INSERT INTO resdex_daily_usage (usage_date, searches, candidates) "
            "VALUES (:d, 0, 0) ON CONFLICT (usage_date) DO NOTHING"
        ),
        {"d": today},
    )
    guard = f" AND {column} + :cost <= :cap" if cap and cap > 0 else ""
    result = await db.execute(
        sa_text(
            f"UPDATE resdex_daily_usage SET {column} = {column} + :cost, "
            f"updated_at = CURRENT_TIMESTAMP WHERE usage_date = :d{guard}"
        ),
        {"cost": cost, "d": today, "cap": cap},
    )
    return (result.rowcount or 0) > 0


async def _used(db: AsyncSession, today, column: str) -> int:
    if column not in _COLUMNS:
        raise ValueError(f"unknown resdex budget column {column!r}")
    row = await db.execute(
        sa_text(f"SELECT {column} FROM resdex_daily_usage WHERE usage_date = :d"), {"d": today},
    )
    return int(row.scalar() or 0)


async def _try(column: str, cap: int, cost: int) -> bool:
    today = _utc_today()
    try:
        factory = get_session_factory(get_settings())
        async with factory() as db:
            allowed = await _reserve(db, today, column, cap, cost)
            await db.commit()
    except Exception as exc:
        logger.warning("resdex_budget_check_failed", column=column, error=str(exc))
        return False  # fail CLOSED
    if not allowed:
        logger.info("resdex_daily_cap_reached", column=column, cap=cap, date=str(today))
    return allowed


async def try_reserve_search() -> bool:
    """Reserve one Resdex search for today. False = cap reached or DB error."""
    return await _try("searches", get_settings().resdex_daily_search_cap, 1)


async def try_reserve_candidates(n: int) -> int:
    """Reserve up to ``n`` candidates; returns how many were granted (0..n) —
    tries n, then fewer, so the last few slots of the day are still used."""
    cap = get_settings().resdex_daily_candidate_cap
    for k in range(n, 0, -1):
        if await _try("candidates", cap, k):
            return k
    return 0


async def usage_today() -> dict[str, int | None]:
    """{"searches", "candidates", "search_cap", "candidate_cap"} — counts are None
    on a DB error."""
    s = get_settings()
    out: dict[str, int | None] = {
        "searches": None, "candidates": None,
        "search_cap": s.resdex_daily_search_cap, "candidate_cap": s.resdex_daily_candidate_cap,
    }
    try:
        factory = get_session_factory(s)
        async with factory() as db:
            today = _utc_today()
            out["searches"] = await _used(db, today, "searches")
            out["candidates"] = await _used(db, today, "candidates")
    except Exception as exc:
        logger.warning("resdex_budget_usage_failed", error=str(exc))
    return out
