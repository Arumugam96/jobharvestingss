"""Tests for the global per-UTC-day Apollo credit budget (app/services/apollo_budget.py).

Exercises the session-taking core (_reserve / _remaining / _reserve_purpose) directly
against the in-memory test DB so the atomic cap behaviour is verified without a real
engine, plus try_consume / remaining_today against a private in-memory DB for the
enrichment sub-cap (APOLLO_ENRICHMENT_DAILY_CAP) layered under the global ceiling.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.services import apollo_budget
from app.services.apollo_budget import _purpose_remaining, _remaining, _reserve, _reserve_purpose


@pytest.mark.asyncio
async def test_reserve_allows_exactly_cap_then_denies(db_session) -> None:
    day = date(2026, 1, 15)
    cap = 3
    # First `cap` reservations succeed...
    assert await _reserve(db_session, day, cap, 1) is True
    assert await _reserve(db_session, day, cap, 1) is True
    assert await _reserve(db_session, day, cap, 1) is True
    # ...the next one is refused.
    assert await _reserve(db_session, day, cap, 1) is False
    # And remaining is 0.
    assert await _remaining(db_session, day, cap) == 0


@pytest.mark.asyncio
async def test_reserve_unlimited_when_cap_zero(db_session) -> None:
    day = date(2026, 1, 16)
    for _ in range(20):
        assert await _reserve(db_session, day, 0, 1) is True
    # Unlimited => remaining is None.
    assert await _remaining(db_session, day, 0) is None


@pytest.mark.asyncio
async def test_reserve_cost_greater_than_one(db_session) -> None:
    day = date(2026, 1, 17)
    cap = 5
    assert await _reserve(db_session, day, cap, 4) is True   # count -> 4
    assert await _reserve(db_session, day, cap, 2) is False  # 4 + 2 > 5 -> denied
    assert await _reserve(db_session, day, cap, 1) is True   # 4 + 1 == 5 -> allowed
    assert await _remaining(db_session, day, cap) == 0


@pytest.mark.asyncio
async def test_remaining_reflects_running_count(db_session) -> None:
    day = date(2026, 1, 18)
    cap = 10
    assert await _remaining(db_session, day, cap) == 10  # no row yet
    await _reserve(db_session, day, cap, 3)
    assert await _remaining(db_session, day, cap) == 7


@pytest.mark.asyncio
async def test_counts_are_per_day(db_session) -> None:
    cap = 2
    d1, d2 = date(2026, 2, 1), date(2026, 2, 2)
    assert await _reserve(db_session, d1, cap, 2) is True
    assert await _reserve(db_session, d1, cap, 1) is False  # d1 exhausted
    assert await _reserve(db_session, d2, cap, 1) is True   # d2 has its own budget


# ── Per-purpose sub-cap (APOLLO_ENRICHMENT_DAILY_CAP) ─────────────────────────

@pytest.mark.asyncio
async def test_reserve_purpose_allows_exactly_cap_then_denies(db_session) -> None:
    day = date(2026, 3, 1)
    assert await _reserve_purpose(db_session, day, "enrichment", 2, 1) is True
    assert await _reserve_purpose(db_session, day, "enrichment", 2, 1) is True
    assert await _reserve_purpose(db_session, day, "enrichment", 2, 1) is False
    assert await _purpose_remaining(db_session, day, "enrichment", 2) == 0
    # Purposes are counted separately; an uncapped one is never denied.
    for _ in range(5):
        assert await _reserve_purpose(db_session, day, "finder", 0, 1) is True
    assert await _purpose_remaining(db_session, day, "finder", 0) is None


@pytest.fixture
async def budget_db(monkeypatch):
    """A private in-memory DB that try_consume / remaining_today commit to."""
    import app.models.harvest_run  # noqa: F401 — registers apollo_daily_usage / apollo_purpose_usage
    from app.models.harvest import Base

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    fac = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr("app.services.apollo_budget.get_session_factory", lambda settings: fac)
    yield fac
    await eng.dispose()


def _caps(monkeypatch, *, global_cap: int, enrichment_cap: int) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "apollo_daily_cap", global_cap)
    monkeypatch.setattr(settings, "apollo_enrichment_daily_cap", enrichment_cap)


async def _counts(fac) -> tuple[int, dict[str, int]]:
    async with fac() as db:
        g = (await db.execute(text("SELECT COALESCE(SUM(count), 0) FROM apollo_daily_usage"))).scalar()
        rows = (await db.execute(text("SELECT purpose, count FROM apollo_purpose_usage"))).all()
    return int(g or 0), {p: int(c) for p, c in rows}


@pytest.mark.asyncio
async def test_enrichment_cap_denies_while_global_allows(budget_db, monkeypatch) -> None:
    _caps(monkeypatch, global_cap=10, enrichment_cap=2)
    assert await apollo_budget.try_consume(1, purpose="enrichment") is True
    assert await apollo_budget.try_consume(1, purpose="enrichment") is True
    assert await apollo_budget.try_consume(1, purpose="enrichment") is False
    # The denied call didn't touch the global counter either.
    assert await _counts(budget_db) == (2, {"enrichment": 2})


@pytest.mark.asyncio
async def test_finder_unaffected_by_exhausted_enrichment_cap(budget_db, monkeypatch) -> None:
    _caps(monkeypatch, global_cap=5, enrichment_cap=1)
    assert await apollo_budget.try_consume(1, purpose="enrichment") is True
    assert await apollo_budget.try_consume(1, purpose="enrichment") is False
    assert await apollo_budget.remaining_today(purpose="enrichment") == 0
    # Finder still has the global headroom (5 - 1 = 4).
    assert await apollo_budget.remaining_today(purpose="finder") == 4
    for _ in range(4):
        assert await apollo_budget.try_consume(1, purpose="finder") is True
    assert await apollo_budget.try_consume(1, purpose="finder") is False   # global ceiling
    assert await _counts(budget_db) == (5, {"enrichment": 1, "finder": 4})


@pytest.mark.asyncio
async def test_global_denial_rolls_back_purpose_bump(budget_db, monkeypatch) -> None:
    _caps(monkeypatch, global_cap=1, enrichment_cap=10)
    assert await apollo_budget.try_consume(1, purpose="finder") is True     # global now full
    assert await apollo_budget.try_consume(1, purpose="enrichment") is False
    assert await apollo_budget.try_consume(1, purpose="finder") is False
    # Neither denied call left a purpose bump (or even the enrichment row) behind.
    assert await _counts(budget_db) == (1, {"finder": 1})


@pytest.mark.asyncio
async def test_remaining_today_is_min_of_global_and_purpose(budget_db, monkeypatch) -> None:
    _caps(monkeypatch, global_cap=10, enrichment_cap=3)
    assert await apollo_budget.remaining_today(purpose="enrichment") == 3   # sub-cap binds
    assert await apollo_budget.remaining_today() == 10                      # global only
    for _ in range(8):
        assert await apollo_budget.try_consume(1, purpose="finder") is True
    assert await apollo_budget.remaining_today(purpose="enrichment") == 2   # global binds
    # Unlimited global: the sub-cap alone decides; both unlimited → None.
    _caps(monkeypatch, global_cap=0, enrichment_cap=3)
    assert await apollo_budget.remaining_today(purpose="enrichment") == 3
    assert await apollo_budget.remaining_today(purpose="finder") is None
    _caps(monkeypatch, global_cap=0, enrichment_cap=0)
    assert await apollo_budget.remaining_today(purpose="enrichment") is None


@pytest.mark.asyncio
async def test_unlimited_enrichment_cap_is_counted_not_capped(budget_db, monkeypatch) -> None:
    _caps(monkeypatch, global_cap=0, enrichment_cap=0)
    for _ in range(3):
        assert await apollo_budget.try_consume(1, purpose="enrichment") is True
    assert await apollo_budget.try_consume(2) is True                      # no purpose
    assert await _counts(budget_db) == (5, {"enrichment": 3})
    usage = await apollo_budget.usage_today()
    assert usage["global_used"] == 5 and usage["enrichment_used"] == 3
    assert usage["global_remaining"] is None and usage["enrichment_remaining"] is None
