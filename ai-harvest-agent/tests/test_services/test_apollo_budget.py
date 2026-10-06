"""Tests for the global per-UTC-day Apollo credit budget (app/services/apollo_budget.py).

Exercises the session-taking core (_reserve / _remaining) directly against the
in-memory test DB so the atomic cap behaviour is verified without a real engine.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services.apollo_budget import _remaining, _reserve


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
