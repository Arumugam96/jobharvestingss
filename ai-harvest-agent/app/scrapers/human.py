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


async def human_type(page: Any, text: str, delay_lo: int = 60, delay_hi: int = 140) -> None:
    """Type into the focused element key by key with a per-key jitter and an
    occasional short pause (a thinking beat), instead of an instant fill.
    Raises like ``page.keyboard.type`` would — callers rely on the text landing."""
    for i, ch in enumerate(text):
        await page.keyboard.type(ch, delay=random.randint(delay_lo, delay_hi))
        if i and i % random.randint(12, 24) == 0:
            await _sleep(page, 150, 450)


async def human_click_or_click(page: Any, element: Any) -> None:
    """``human_click`` (gradual pointer move + click near the centre), then a
    plain ``element.click()`` if it reports no click was issued — that last
    attempt RAISES like Playwright would, so a missing element still surfaces."""
    if not await human_click(page, element):
        await element.click()


async def human_read_dwell(
    page: Any, total_s: float, *, rounds: int = 3, container: Any = None,
) -> int:
    """Look through a results list like a person: ``rounds`` short downward
    scrolls (a step or two of ``human_scroll`` each) separated by reading
    pauses, spending about ``total_s`` seconds in all. Never clicks anything.
    Best-effort — never raises. Returns the number of scroll steps performed."""
    import time

    # Budget = total_s minus the reading beats requested minus the wall time the
    # scrolls took (their own pauses), so the total holds on any page clock.
    left = max(0.0, float(total_s))
    n = max(1, int(rounds))
    steps = 0
    for i in range(n):
        if left <= 0:
            break
        # A reading beat before each scroll: a jittered share of what's left.
        beat = min(left, left / (n - i + 1) * random.uniform(0.6, 1.4))
        await _sleep(page, int(beat * 1000), int(beat * 1000))
        left -= beat
        started = time.monotonic()
        try:
            steps += await human_scroll(
                page, container, max_rounds=random.randint(1, 2),
                step_min=250, step_max=650, pause_lo=250, pause_hi=700,
            )
        except Exception:
            pass
        left -= time.monotonic() - started
    if left > 0:
        await _sleep(page, int(left * 1000), int(left * 1000))
    return steps
