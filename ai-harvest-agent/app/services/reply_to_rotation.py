"""Daily Reply-To rotation for automated outreach.

When OUTREACH_AUTO_REPLY_TO holds two or more addresses, the end-of-harvest
auto-outreach flow (app/services/auto_outreach_service.py) rotates the *primary*
Reply-To through that list one address per calendar day: day 1 → index 0, day 2 →
index 1, …, wrapping around. "Day 1" is auto-anchored to the first automated send —
the first time a rotation index is requested and no anchor exists yet, today's date
is stamped as the anchor, so index 0 (the first address) is used that day.

The anchor is a single ``YYYY-MM-DD`` date persisted to
``data/config/reply_to_rotation.json`` (same directory + JSON-store pattern as
config_service.py). "Today" is computed in the harvest schedule's timezone
(HarvestConfig.schedule.timezone, default Asia/Kolkata) so the daily flip lines up
with the scheduled run rather than with UTC midnight.

Everything here is best-effort and never raises: on any file/parse/timezone error it
falls back to a stateless calendar-date parity so a send is never blocked by rotation
state. Callers apply ``% len(addresses)`` to the returned index.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytz
import structlog

from app.services.config_service import ConfigService

logger = structlog.get_logger(__name__)

# Anchored to the ai-harvest-agent project root (parent of app/services/), matching
# config_service._CONFIG_PATH, so the anchor file resolves to the same data/config/
# dir regardless of the process CWD.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ANCHOR_PATH = _PROJECT_ROOT / "data" / "config" / "reply_to_rotation.json"

# Common timezone abbreviations → IANA names (mirrors scheduler_service._TZ_MAP for
# the ones a user is likely to type). Full IANA names pass through unchanged.
_TZ_ALIASES = {"IST": "Asia/Kolkata", "UTC": "UTC", "GMT": "Europe/London"}
_DEFAULT_TZ = "Asia/Kolkata"


def _today() -> date:
    """Current date in the harvest schedule's timezone. Falls back to the schedule
    default, then to the naive local date if the timezone can't be resolved."""
    tz_name = _DEFAULT_TZ
    try:
        tz_name = (ConfigService().load().schedule.timezone or _DEFAULT_TZ).strip()
    except Exception:
        pass  # config unreadable — use the default tz
    tz_name = _TZ_ALIASES.get(tz_name.upper(), tz_name)
    try:
        return datetime.now(pytz.timezone(tz_name)).date()
    except Exception:
        return datetime.now().date()


def rotation_index() -> int:
    """Return the 0-based rotation index for today (days elapsed since the anchor).

    Auto-anchors on first use: when no valid anchor is stored, today's date is
    persisted and 0 is returned (so the first day uses the first address). The
    caller applies ``% len(addresses)``. Best-effort — on any error it returns a
    stateless calendar-date parity index instead of raising."""
    try:
        today = _today()
        anchor = _read_anchor()
        if anchor is None:
            _write_anchor(today)
            logger.info("reply_to_rotation_anchored", anchor=today.isoformat())
            return 0
        return (today - anchor).days
    except Exception as exc:  # never block a send over rotation bookkeeping
        logger.warning("reply_to_rotation_fallback", error=str(exc))
        # Stateless parity: alternates every calendar day regardless of anchor state.
        return date.today().toordinal()


def _read_anchor() -> date | None:
    """Read and parse the stored anchor date, or None when absent/malformed."""
    if not _ANCHOR_PATH.exists():
        return None
    raw = json.loads(_ANCHOR_PATH.read_text(encoding="utf-8"))
    anchor = (raw.get("anchor") or "").strip()
    return date.fromisoformat(anchor) if anchor else None


def _write_anchor(value: date) -> None:
    """Persist the anchor date (creates the data/config/ dir if needed)."""
    _ANCHOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    _ANCHOR_PATH.write_text(
        json.dumps({"anchor": value.isoformat()}, indent=2),
        encoding="utf-8",
    )
