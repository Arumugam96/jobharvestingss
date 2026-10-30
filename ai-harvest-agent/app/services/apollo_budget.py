"""Global per-UTC-day Apollo credit-call budget — the hard daily cap backstop.

Apollo credits are account-wide (one API key), a harvest runs in a detached task /
proactor thread, and scheduled runs are separate triggers, so an in-memory counter is
useless — the cap MUST be DB-backed to hold across runs/processes. This module owns
the `apollo_daily_usage` counter (one row per UTC date) and exposes a single atomic
``try_consume`` that ApolloClient calls BEFORE every credit-spending request. When the
day's budget is exhausted, ``try_consume`` returns False and the client short-circuits
to its empty/no-match result (never raises).

Three caps bound Apollo spend:
  * ``APOLLO_DAILY_CAP`` — the account-wide ceiling; EVERY credit reserves against it.
  * ``APOLLO_ENRICHMENT_DAILY_CAP`` — an additional sub-cap that only harvest /
    maintenance contact enrichment (``purpose="enrichment"``) must also pass, tracked
    per purpose in `apollo_purpose_usage`. When it's exhausted enrichment stops, but
    Recruiter Finder (``purpose="finder"``, counted but not sub-capped) can still use
    the remaining global headroom.
  * ``RECRUITER_FINDER_DAILY_CAP`` — Recruiter Finder's own per-tenant cap, enforced
    separately in app/services/recruiter_finder_budget.py.

Atomicity: the gate is a single conditional ``UPDATE ... WHERE count + cost <= cap``
whose row-count tells us whether the bump was allowed — row-locked in Postgres and
globally serialized in SQLite, so two concurrent callers can't both pass at the limit.

Best-effort: a DB error fails **open** (allows the call, logs a warning), consistent
with the app's other best-effort DB helpers (db_read/db_write) — the per-run caps still
bound usage, and a DB outage means little is running anyway. ``cap <= 0`` = unlimited.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

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


async def _reserve_purpose(db: AsyncSession, today, purpose: str, cap: int, cost: int) -> bool:
    """Per-purpose twin of ``_reserve``: atomic conditional increment of the
    (``today``, ``purpose``) row in apollo_purpose_usage (no commit — the caller owns
    the transaction). Returns True when the bump was allowed. ``cap <= 0`` = unlimited
    (counted for observability, never denied)."""
    await db.execute(
        sa_text(
            "INSERT INTO apollo_purpose_usage (usage_date, purpose, count) "
            "VALUES (:d, :p, 0) ON CONFLICT (usage_date, purpose) DO NOTHING"
        ),
        {"d": today, "p": purpose},
    )
    if cap and cap > 0:
        result = await db.execute(
            sa_text(
                "UPDATE apollo_purpose_usage "
                "SET count = count + :cost, updated_at = CURRENT_TIMESTAMP "
                "WHERE usage_date = :d AND purpose = :p AND count + :cost <= :cap"
            ),
            {"cost": cost, "d": today, "p": purpose, "cap": cap},
        )
        return (result.rowcount or 0) > 0
    # Unlimited — count for observability but never deny.
    await db.execute(
        sa_text(
            "UPDATE apollo_purpose_usage "
            "SET count = count + :cost, updated_at = CURRENT_TIMESTAMP "
            "WHERE usage_date = :d AND purpose = :p"
        ),
        {"cost": cost, "d": today, "p": purpose},
    )
    return True


async def _purpose_used(db: AsyncSession, today, purpose: str) -> int:
    """Credits ``purpose`` has reserved today (0 when it has no row yet)."""
    row = await db.execute(
        sa_text("SELECT count FROM apollo_purpose_usage WHERE usage_date = :d AND purpose = :p"),
        {"d": today, "p": purpose},
    )
    return int(row.scalar() or 0)


async def _purpose_remaining(db: AsyncSession, today, purpose: str, cap: int | None) -> int | None:
    """Credits left under ``purpose``'s sub-cap for ``today`` (clamped ≥ 0), None when
    the purpose is uncapped."""
    if not cap or cap <= 0:
        return None
    return max(0, cap - await _purpose_used(db, today, purpose))


def _purpose_cap(purpose: str | None) -> int | None:
    """The daily sub-cap for ``purpose``: settings.apollo_enrichment_daily_cap for
    "enrichment"; None (counted, never capped) for anything else, e.g. "finder"."""
    if purpose == "enrichment":
        return get_settings().apollo_enrichment_daily_cap
    return None


def _min_remaining(a: int | None, b: int | None) -> int | None:
    """min() where None = unlimited."""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


async def try_consume(
    cost: int = 1, *, cap: int | None = None, purpose: str | None = None
) -> bool:
    """Atomically reserve ``cost`` Apollo credit-calls for today, if the daily caps
    allow. Returns True when reserved (caller may spend), False when a cap is reached
    (caller must skip). ``cap`` defaults to settings.apollo_daily_cap; ``cap <= 0``
    means unlimited (always allowed, still counted). With ``purpose``, the per-purpose
    row is reserved first (sub-capped for "enrichment") in the SAME transaction — if
    either the purpose or the global gate denies, both bumps are rolled back.
    Fail-open on any DB error.
    """
    settings = get_settings()
    cap = settings.apollo_daily_cap if cap is None else cap
    today = _utc_today()
    try:
        factory = get_session_factory(settings)
        async with factory() as db:
            if purpose:
                p_cap = _purpose_cap(purpose) or 0
                if not await _reserve_purpose(db, today, purpose, p_cap, cost):
                    await db.rollback()
                    logger.info(
                        "apollo_enrichment_cap_reached",
                        purpose=purpose, cap=p_cap, date=str(today),
                    )
                    return False
            if not await _reserve(db, today, cap, cost):
                await db.rollback()  # also undoes the purpose bump above
                logger.info("apollo_daily_cap_reached", cap=cap, date=str(today))
                return False
            await db.commit()
            return True
    except Exception as exc:
        logger.warning("apollo_budget_check_failed", error=str(exc))
        return True  # fail-open: don't block enrichment on a DB blip


async def remaining_today(*, cap: int | None = None, purpose: str | None = None) -> int | None:
    """Credits left today: the global ``cap - count`` (clamped ≥ 0) and, for a capped
    ``purpose`` (i.e. "enrichment"), the min of that and the sub-cap's headroom. None
    when unlimited (both caps ``<= 0``) or on any DB error, so callers treat it as "go
    ahead" and let ``try_consume`` be the real gate. Used for pre-flight skips /
    logging."""
    settings = get_settings()
    cap = settings.apollo_daily_cap if cap is None else cap
    p_cap = _purpose_cap(purpose)
    global_capped = bool(cap and cap > 0)
    purpose_capped = bool(purpose and p_cap and p_cap > 0)
    if not global_capped and not purpose_capped:
        return None
    today = _utc_today()
    try:
        factory = get_session_factory(settings)
        async with factory() as db:
            g_left = await _remaining(db, today, cap) if global_capped else None
            p_left = (
                await _purpose_remaining(db, today, purpose, p_cap) if purpose_capped else None
            )
            return _min_remaining(g_left, p_left)
    except Exception as exc:
        logger.warning("apollo_budget_remaining_failed", error=str(exc))
        return None


async def usage_today() -> dict:
    """Today's Apollo usage for display: the global and enrichment counters with their
    caps and headroom (``*_remaining`` None = unlimited; caps as configured, ``<= 0`` =
    unlimited). Zero usage on any DB error — display-only, never a gate."""
    settings = get_settings()
    g_cap = settings.apollo_daily_cap
    e_cap = settings.apollo_enrichment_daily_cap
    today = _utc_today()
    g_used = e_used = 0
    try:
        factory = get_session_factory(settings)
        async with factory() as db:
            row = await db.execute(
                sa_text("SELECT count FROM apollo_daily_usage WHERE usage_date = :d"),
                {"d": today},
            )
            g_used = int(row.scalar() or 0)
            e_used = await _purpose_used(db, today, "enrichment")
    except Exception as exc:
        logger.warning("apollo_budget_usage_failed", error=str(exc))
    return {
        "global_used": g_used,
        "global_cap": g_cap,
        "global_remaining": max(0, g_cap - g_used) if g_cap and g_cap > 0 else None,
        "enrichment_used": e_used,
        "enrichment_cap": e_cap,
        "enrichment_remaining": max(0, e_cap - e_used) if e_cap and e_cap > 0 else None,
    }


async def usage_this_month() -> dict:
    """Current calendar month's cumulative Apollo usage for display in the
    harvest report email. Sums ``apollo_daily_usage.count`` from the 1st of
    the month through today. Returns ``monthly_cap`` and ``monthly_remaining``
    when ``APOLLO_MONTHLY_CAP > 0``; ``monthly_remaining`` is None when
    uncapped. Zero usage on any DB error."""
    settings = get_settings()
    m_cap = settings.apollo_monthly_cap
    today = _utc_today()
    month_start = date(today.year, today.month, 1)
    m_used = 0
    try:
        factory = get_session_factory(settings)
        async with factory() as db:
            row = await db.execute(
                sa_text(
                    "SELECT COALESCE(SUM(count), 0) FROM apollo_daily_usage"
                    " WHERE usage_date >= :start AND usage_date <= :end"
                ),
                {"start": month_start, "end": today},
            )
            m_used = int(row.scalar() or 0)
    except Exception as exc:
        logger.warning("apollo_budget_month_usage_failed", error=str(exc))
    return {
        "monthly_used": m_used,
        "monthly_cap": m_cap,
        "monthly_remaining": max(0, m_cap - m_used) if m_cap and m_cap > 0 else None,
    }
