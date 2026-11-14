"""page.evaluate() with a timeout.

Playwright's Page/Frame/ElementHandle ``evaluate`` has no timeout of its own: on
a tab whose renderer is frozen (busy script, stuck layout — not crashed) the
call waits forever, and because the run's Stop button is only honoured between
jobs, the whole harvest hangs until the server is restarted (losing the
unflushed tail). Every harvest-path scraper calls this instead.

A timeout raises PageEvaluateTimeout (a TimeoutError, so the callers' existing
``except Exception`` handling treats it like any other failed evaluate).
"""
from __future__ import annotations

import asyncio
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class PageEvaluateTimeout(TimeoutError):
    """page.evaluate() didn't return within the configured ceiling."""


def _default_timeout_s() -> float:
    """Configured page_evaluate_timeout_s, or 30 s if settings can't be loaded."""
    try:
        from app.config import get_settings

        return float(get_settings().page_evaluate_timeout_s)
    except Exception:
        return 30.0


async def page_evaluate(
    target: Any,
    expression: str,
    arg: Any = None,
    *,
    timeout_s: float | None = None,
) -> Any:
    """``await target.evaluate(expression[, arg])`` bounded by ``timeout_s``
    (default: settings.page_evaluate_timeout_s). ``target`` is a Page, Frame
    or ElementHandle. ``arg`` is only passed when given, exactly like Playwright."""
    limit = timeout_s if timeout_s is not None else _default_timeout_s()
    coro = target.evaluate(expression) if arg is None else target.evaluate(expression, arg)
    if not limit or limit <= 0:
        return await coro
    try:
        return await asyncio.wait_for(coro, timeout=limit)
    except asyncio.TimeoutError as exc:
        logger.warning("page_evaluate_timeout", timeout_s=limit,
                       script=(expression or "")[:80].replace("\n", " "))
        raise PageEvaluateTimeout(f"page.evaluate timed out after {limit:g}s") from exc
