"""Small browser-agnostic helpers shared by the harvest agents."""
from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable

import structlog

logger = structlog.get_logger(__name__)


def env_max_jobs_cap(env_var: str) -> int | None:
    """Optional testing cap read from `env_var` (e.g. LINKEDIN_TEST_MAX_JOBS,
    NAUKRI_TEST_MAX_JOBS) in the environment (.env). When set to a positive
    integer it overrides FiltersConfig.max_jobs with a lower value so a test run
    doesn't harvest the full result set. Unset/blank/non-positive/invalid → None
    (normal max_jobs behaviour)."""
    raw = os.getenv(env_var, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning("test_max_jobs_invalid", env_var=env_var, value=raw)
        return None
    return value if value > 0 else None


async def retry_async(
    coro_fn: Callable[[], Awaitable[Any]],
    retries: int = 3,
    delay_s: float = 2.0,
) -> Any:
    """Await `coro_fn()` up to `retries` times, sleeping `delay_s` between
    attempts; re-raises the last exception if every attempt fails."""
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return await coro_fn()
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                logger.debug("retry_attempt", attempt=attempt, error=str(exc))
                await asyncio.sleep(delay_s)
    raise last_exc  # type: ignore[misc]
