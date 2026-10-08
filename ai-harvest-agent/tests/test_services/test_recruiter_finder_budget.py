"""Tests for the per-tenant Apollo budget gate (app/services/recruiter_finder_budget.py).

Exercises the session-taking core (_reserve) directly against the in-memory test DB,
proving the atomic cap holds AND that each tenant has its own independent daily pool.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services.recruiter_finder_budget import _reserve


@pytest.mark.asyncio
async def test_each_tenant_has_its_own_daily_pool(db_session) -> None:
    day = date(2026, 1, 15)
    cap = 3
    # Tenant A spends its whole pool...
    assert await _reserve(db_session, day, "client_us", cap, 1) is True
    assert await _reserve(db_session, day, "client_us", cap, 1) is True
    assert await _reserve(db_session, day, "client_us", cap, 1) is True
    assert await _reserve(db_session, day, "client_us", cap, 1) is False
    # ...tenant B is completely unaffected on the same day.
    assert await _reserve(db_session, day, "client_in", cap, 1) is True
    assert await _reserve(db_session, day, "client_in", cap, 2) is True
    assert await _reserve(db_session, day, "client_in", cap, 1) is False


@pytest.mark.asyncio
async def test_cost_greater_than_one_and_boundary(db_session) -> None:
    day = date(2026, 2, 1)
    cap = 5
    assert await _reserve(db_session, day, "t", cap, 4) is True   # count -> 4
    assert await _reserve(db_session, day, "t", cap, 2) is False  # 4 + 2 > 5 -> denied
    assert await _reserve(db_session, day, "t", cap, 1) is True   # 4 + 1 == 5 -> allowed


@pytest.mark.asyncio
async def test_unlimited_when_cap_zero(db_session) -> None:
    day = date(2026, 2, 2)
    for _ in range(12):
        assert await _reserve(db_session, day, "u", 0, 1) is True


@pytest.mark.asyncio
async def test_counts_are_per_day(db_session) -> None:
    cap = 2
    d1, d2 = date(2026, 3, 1), date(2026, 3, 2)
    assert await _reserve(db_session, d1, "t", cap, 2) is True
    assert await _reserve(db_session, d1, "t", cap, 1) is False  # d1 exhausted
    assert await _reserve(db_session, d2, "t", cap, 1) is True   # d2 has its own budget
