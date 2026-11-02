"""Apollo's own credit figures for the harvest report email.

The local counters in app/services/apollo_budget.py are the spend GATE — they're
reserved before every call and settled to what each response implies. But they can't
see every charge (Apollo bills some calls without saying so in the response, and
credits spent in the Apollo UI never pass through this app), so for DISPLAY the report
asks Apollo itself: ``POST /usage_stats/credit_usage_stats`` (0 credits; needs a master
API key). On the unified credit plan ``lead_credit`` is the one shared pool the Apollo
dashboard shows ("498 of 2,580 credits used").

Apollo only reports usage for the current billing cycle (not per day), so:
  * cycle used / limit / remaining — straight from the response;
  * credits this run — the reading at report time minus the reading taken at run start;
  * credits today — the reading now minus the last snapshot at or before IST midnight
    (``snapshot_job`` stores one every 15 min in ``apollo_credit_snapshots``).
A billing-cycle reset between two readings makes the later ``consumed`` the delta.

Best-effort throughout: any API/DB error returns None and the report falls back to the
local counters. Never raises.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import structlog
from sqlalchemy import delete, select

from app.config import Settings, get_settings
from app.core.dependencies import get_session_factory

logger = structlog.get_logger(__name__)

_CREDIT_TYPE = "lead_credit"
_IST = timezone(timedelta(hours=5, minutes=30), "IST")
_SNAPSHOT_RETENTION = timedelta(days=90)


def _utc(value: datetime | None) -> datetime | None:
    """Aware UTC datetime (naive values — e.g. read back from SQLite — are UTC)."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def parse_credit_usage(data: object) -> dict | None:
    """``{consumed, limit, left_over, cycle_start, cycle_end}`` for ``lead_credit`` from a
    credit_usage_stats response, or None when the shape is unexpected."""
    if not isinstance(data, dict):
        return None
    pool = (data.get("credit_usage_stats") or {}).get(_CREDIT_TYPE)
    if not isinstance(pool, dict) or pool.get("consumed") is None:
        return None
    consumed = int(pool.get("consumed") or 0)
    limit = int(pool.get("limit") or 0)
    left_over = pool.get("left_over")
    cycle = data.get("current_credit_cycle") or {}
    return {
        "consumed": consumed,
        "limit": limit,
        "left_over": int(left_over) if left_over is not None else max(0, limit - consumed),
        "cycle_start": _parse_dt(cycle.get("start_date")),
        "cycle_end": _parse_dt(cycle.get("end_date")),
    }


async def fetch_credit_usage(settings: Settings | None = None) -> dict | None:
    """Apollo's current ``lead_credit`` usage (see ``parse_credit_usage``), or None when
    no key is configured / the call fails / the key lacks access (403)."""
    settings = settings or get_settings()
    if not settings.apollo_api_key:
        return None
    url = f"{settings.apollo_base_url.rstrip('/')}/usage_stats/credit_usage_stats"
    try:
        async with httpx.AsyncClient(timeout=settings.apollo_timeout_s) as client:
            resp = await client.post(
                url,
                headers={
                    "x-api-key": settings.apollo_api_key,
                    "Content-Type": "application/json",
                    "Cache-Control": "no-cache",
                },
                json={},
            )
        if resp.status_code != 200:
            logger.warning("apollo_credit_usage_http_error", status=resp.status_code)
            return None
        usage = parse_credit_usage(resp.json())
    except Exception as exc:
        logger.warning("apollo_credit_usage_fetch_failed", error=str(exc))
        return None
    if usage is None:
        logger.warning("apollo_credit_usage_unexpected_shape")
    return usage


def credits_between(base: dict | None, now: dict | None) -> int | None:
    """Credits charged between two readings; a cycle reset in between → ``now``'s
    consumed (everything since the reset)."""
    if not base or not now:
        return None
    if _utc(base.get("cycle_start")) != _utc(now.get("cycle_start")):
        return int(now["consumed"])
    return max(0, int(now["consumed"]) - int(base["consumed"]))


def _row_to_reading(row) -> dict:
    return {"consumed": row.consumed, "limit": row.credit_limit, "cycle_start": row.cycle_start}


async def take_snapshot() -> dict | None:
    """Fetch Apollo's reading and store it (``taken_at`` = now). Returns the reading
    (even when the DB write fails), or None when Apollo couldn't be read."""
    from app.models.harvest_run import ApolloCreditSnapshotORM

    usage = await fetch_credit_usage()
    if usage is None:
        return None
    usage["taken_at"] = datetime.now(timezone.utc)
    try:
        factory = get_session_factory(get_settings())
        async with factory() as db:
            db.add(ApolloCreditSnapshotORM(
                taken_at=usage["taken_at"], consumed=usage["consumed"],
                credit_limit=usage["limit"], cycle_start=usage["cycle_start"],
            ))
            await db.commit()
    except Exception as exc:
        logger.warning("apollo_credit_snapshot_store_failed", error=str(exc))
    return usage


async def snapshot_job() -> None:
    """Scheduler tick: store a reading and prune readings older than the retention."""
    from app.models.harvest_run import ApolloCreditSnapshotORM as S

    if await take_snapshot() is None:
        return
    try:
        factory = get_session_factory(get_settings())
        async with factory() as db:
            cutoff = datetime.now(timezone.utc) - _SNAPSHOT_RETENTION
            await db.execute(delete(S).where(S.taken_at < cutoff))
            await db.commit()
    except Exception as exc:
        logger.warning("apollo_credit_snapshot_prune_failed", error=str(exc))


async def _latest_at_or_before(db, when: datetime) -> dict | None:
    from app.models.harvest_run import ApolloCreditSnapshotORM as S

    row = (await db.execute(
        select(S).where(S.taken_at <= when).order_by(S.taken_at.desc()).limit(1)
    )).scalar_one_or_none()
    return _row_to_reading(row) if row else None


async def _earliest_after(db, when: datetime) -> dict | None:
    from app.models.harvest_run import ApolloCreditSnapshotORM as S

    row = (await db.execute(
        select(S).where(S.taken_at > when).order_by(S.taken_at.asc()).limit(1)
    )).scalar_one_or_none()
    return _row_to_reading(row) if row else None


def ist_day_start(now: datetime | None = None) -> datetime:
    """Today's 00:00 IST as an aware UTC datetime."""
    local = (now or datetime.now(timezone.utc)).astimezone(_IST)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


async def report_usage(
    run_baseline: dict | None = None, run_started_at: datetime | None = None
) -> dict | None:
    """Apollo figures for the report: the cycle reading plus ``today`` (since 00:00 IST)
    and ``run`` (since ``run_baseline``, else the last snapshot before
    ``run_started_at``). ``today`` / ``run`` are None when no baseline exists. None
    overall when Apollo can't be read."""
    now = await take_snapshot()
    if now is None:
        return None
    result = {**now, "today": None, "run": credits_between(run_baseline, now)}
    try:
        factory = get_session_factory(get_settings())
        async with factory() as db:
            day_start = ist_day_start(now["taken_at"])
            # No reading before midnight (fresh install / long outage) → the first one
            # after it: understates by whatever was spent before it, never overstates.
            day_base = await _latest_at_or_before(db, day_start) or await _earliest_after(
                db, day_start
            )
            result["today"] = credits_between(day_base, now)
            if result["run"] is None and run_started_at is not None:
                run_base = await _latest_at_or_before(db, _utc(run_started_at))
                result["run"] = credits_between(run_base, now)
    except Exception as exc:
        logger.warning("apollo_credit_usage_baseline_failed", error=str(exc))
    return result
