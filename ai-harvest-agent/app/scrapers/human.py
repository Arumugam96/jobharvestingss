"""Human-like Playwright interaction helpers.

Small, dependency-free jitter over mouse movement, clicking, and scrolling so
automated navigation doesn't present a robotic, instantaneous, jump-to-bottom
interaction pattern (a bot signal that contributed to a LinkedIn account being
restricted).

Every function is best-effort: it is wrapped so a failure degrades to the plain
Playwright action (or a no-op) and never raises into the caller's scrape flow.
Works with either a Playwright Locator or an ElementHandle where an element is
accepted (both expose ``bounding_box`` / ``scroll_into_view_if_needed`` /
``click``).
"""
from __future__ import annotations

import random
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


async def _sleep(page: Any, lo: int, hi: int) -> None:
    """Randomized pause via the page clock; swallows errors."""
    try:
        await page.wait_for_timeout(random.randint(lo, hi))
    except Exception:
        pass


async def human_mouse_move(page: Any, x: float, y: float, steps: int = 0) -> None:
    """Move the pointer to (x, y) over a few small steps instead of teleporting.
    Best-effort — a driver without a real pointer just no-ops."""
    try:
        await page.mouse.move(x, y, steps=steps or random.randint(3, 6))
    except Exception:
        pass


async def human_click(page: Any, element: Any) -> bool:
    """Scroll `element` into view, glide the pointer toward it with a brief pause,
    then click near its centre with a little offset jitter. Falls back to a plain
    ``element.click()`` on any failure. Returns True if a click was issued."""
    try:
        await element.scroll_into_view_if_needed(timeout=2_000)
    except Exception:
        pass

    box = None
    try:
        box = await element.bounding_box()
    except Exception:
        box = None

    if box and box.get("width") and box.get("height"):
        try:
            cx = box["x"] + box["width"] * random.uniform(0.35, 0.65)
            cy = box["y"] + box["height"] * random.uniform(0.35, 0.65)
            await human_mouse_move(page, cx, cy)
            await _sleep(page, 120, 380)
            await page.mouse.click(cx, cy)
            return True
        except Exception:
            pass

    try:
        await element.click()
        return True
    except Exception:
        return False


async def human_scroll(
    page: Any,
    container: Any = None,
    max_rounds: int = 40,
    step_min: int = 500,
    step_max: int = 1_100,
    pause_lo: int = 350,
    pause_hi: int = 900,
) -> int:
    """Scroll `container` (an ElementHandle) — or the window when None — DOWN in
    randomized increments with human-like pauses, until the scrollable content
    stops growing AND the bottom has been reached (stable for two checks), or
    ``max_rounds`` is hit. Replaces jump-to-bottom ``scrollTo(scrollHeight)``,
    which is an obvious automation tell.

    Scroll POSITION at termination doesn't matter to callers that read the DOM
    (cards are extracted by selector, not viewport visibility) — the goal is to
    progressively trigger the lazy-loader until every item is in the DOM. Returns
    the number of scroll steps performed. Best-effort: any evaluation error ends
    the loop rather than raising.
    """
    prev_h = -1
    stable = 0
    rounds = 0
    for _ in range(max_rounds):
        try:
            if container is not None:
                geo = await container.evaluate(
                    "el => ({h: el.scrollHeight, top: el.scrollTop, ch: el.clientHeight})"
                )
            else:
                geo = await page.evaluate(
                    "() => ({h: document.body.scrollHeight, "
                    "top: (document.scrollingElement||document.documentElement).scrollTop, "
                    "ch: window.innerHeight})"
                )
        except Exception:
            break

        h = int(geo.get("h") or 0)
        at_bottom = (int(geo.get("top") or 0) + int(geo.get("ch") or 0)) >= (h - 80)

        if h == prev_h and at_bottom:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        prev_h = h

        step = random.randint(step_min, step_max)
        try:
            if container is not None:
                await container.evaluate("(el, dy) => el.scrollBy(0, dy)", step)
            else:
                # A real wheel gesture over the viewport plus a scrollBy, so it
                # works whether the page scrolls on the window or an inner element.
                try:
                    await page.mouse.wheel(0, step)
                except Exception:
                    pass
                await page.evaluate("(dy) => window.scrollBy(0, dy)", step)
        except Exception:
            break

        await _sleep(page, pause_lo, pause_hi)
        rounds += 1

    return rounds
