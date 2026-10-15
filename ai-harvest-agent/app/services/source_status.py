"""Per-source live status for a multi-source harvest run.

The orchestrator (app/agents/orchestrator_agent.py) runs LinkedIn / Naukri / Dice
concurrently. Before this module they all shared one run-level ``message`` (so
Naukri's progress overwrote LinkedIn's and vice versa) and per-source counts were
only filled in at the end of collection. ``SourceStatusTracker`` keeps one entry
per ENABLED source::

    {"naukri": {"state": "running", "count": 40, "message": "Naukri: page 2 done…",
                "reason": "", "error": "", "updated_at": "2026-10-10T06:00:00+00:00"}}

state ∈ pending | running | done | failed | skipped. ``reason`` is a short,
UI-friendly cause ("LLM unavailable"); ``error`` the full detail (provider message).

Each entry also carries optional live-progress keys (see PROGRESS_KEYS: phase,
reported_total, listed, skipped, target, cap, done, in_batch, batch_size,
batches, page, total_pages, window_hours — all None until reported) fed by the
agents' ``on_progress`` callback through ``progress()`` / ``set_listing()`` /
``tick()``; ``add_jobs()`` (a flushed persist batch) resets ``in_batch`` and
bumps ``batches``. A tick that only moves ``done`` / ``in_batch`` is throttled
like a message; listing/phase changes are emitted immediately.

The orchestrator routes every source's on_status / on_batch through per-source
wrappers into the tracker; the tracker hands snapshots to an ``on_change``
callback (the run route persists them to harvest_runs.source_status + the live
*_count columns, and to JobTracker). Writes are throttled: a state or count change
is emitted immediately, a message-only change at most once per ``min_interval_s``
(a throttled message is sent by a single trailing flush once the interval passes,
or rides along with an earlier forced emit). Call ``aclose()`` when collection
ends — it cancels that trailing timer (no task leaks past the run's event loop)
and flushes anything still unsent.
"""
from __future__ import annotations

import asyncio
import copy
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable

import structlog

logger = structlog.get_logger(__name__)

SOURCE_STATES = ("pending", "running", "done", "failed", "skipped")
TERMINAL_STATES = frozenset({"done", "failed", "skipped"})

# Short, user-facing reasons (the detailed provider/exception text goes in "error").
REASON_LLM_UNAVAILABLE = "LLM unavailable"
REASON_LOGIN_REQUIRED = "Login required"
REASON_ERROR = "Error"
REASON_BLOCKED = "Blocked by Naukri"

OnChange = Callable[[dict[str, dict[str, Any]]], Awaitable[None]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Optional live-progress keys (the Rule Engine "Live Harvest" card). All default
# to None ("unknown / not reported") so old consumers and old snapshots keep
# working; the card falls back to the compact state/count/message row when they
# are absent.
#   phase           listing | details | done
#   reported_total  the site's own match count for the filter window (corner number)
#   listed          result cards enumerated so far (title + URL only)
#   skipped         postings skipped as already harvested in the lookback window
#   target          jobs this source will try to fetch: min(found − skipped, cap)
#   cap             the source's max-jobs safety cap
#   done            detail pages finished (flushed + still in the pending batch)
#   in_batch        jobs waiting in the current (unflushed) persist batch
#   batch_size      settings.harvest_persist_batch_size
#   batches         batches flushed so far (bumped by add_jobs)
#   page            current results page (1-based)
#   total_pages     total results pages when the site reports it (Naukri)
#   window_hours    the run's search_window_hours filter ("Past 24h" chip)
PROGRESS_KEYS: tuple[str, ...] = (
    "phase", "reported_total", "listed", "skipped", "target", "cap", "done",
    "in_batch", "batch_size", "batches", "page", "total_pages", "window_hours",
    "resume_at",
)
# Keys whose change is just "one more job extracted" — throttled like a message.
# Any other progress key changing (phase, listing totals, …) is emitted at once.
TICK_KEYS = frozenset({"done", "in_batch"})
# cooling_down       — paused after a rate-limit/CAPTCHA (resume_at = epoch seconds)
# waiting_for_human  — a CAPTCHA must be solved in the Live Browser (resume_at = deadline)
PROGRESS_PHASES = ("listing", "details", "cooling_down", "waiting_for_human", "done")


def _entry(state: str = "pending", message: str = "") -> dict[str, Any]:
    entry: dict[str, Any] = {
        "state": state,
        "count": 0,
        "message": message,
        "reason": "",
        "error": "",
        "updated_at": _now_iso(),
    }
    for key in PROGRESS_KEYS:
        entry[key] = None
    return entry


def _clean_progress(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only known progress keys with usable values (ints ≥ 0, a known phase).
    None values are dropped — "not reported this time", never "clear the field".
    Never raises: a malformed progress payload must not break a scrape."""
    out: dict[str, Any] = {}
    for key, value in (payload or {}).items():
        if key not in PROGRESS_KEYS or value is None:
            continue
        if key == "phase":
            if value in PROGRESS_PHASES:
                out[key] = value
            continue
        try:
            n = int(value)
        except (TypeError, ValueError):
            continue
        out[key] = max(0, n)
    return out


def initial_source_status(sources: Iterable[str]) -> dict[str, dict[str, Any]]:
    """A fresh all-``pending`` status map for the given enabled source keys."""
    return {s: _entry("pending", "Waiting to start") for s in sources}


def per_source_counts(snapshot: dict[str, dict[str, Any]] | None) -> dict[str, int]:
    """{"naukri": n, ...} from a status snapshot — feeds the legacy
    naukri/linkedin/dice count fields so they rise live too."""
    out: dict[str, int] = {}
    for src, entry in (snapshot or {}).items():
        try:
            out[src] = int((entry or {}).get("count") or 0)
        except (TypeError, ValueError):
            out[src] = 0
    return out


def finalize_source_status(
    snapshot: dict[str, dict[str, Any]] | None,
    run_status: str,
    run_error: str = "",
) -> dict[str, dict[str, Any]]:
    """Serialisation-time guard: once the RUN has ended, no source may still claim
    to be pending/running (e.g. the process restarted mid-run, or the run failed
    before a source finished). Those entries are reported as ``failed`` (failed
    run, carrying the run's error) or ``skipped`` (stopped / completed run).
    Returns a new dict; a still-running run is returned unchanged (copied)."""
    out = copy.deepcopy(snapshot or {})
    if not out or run_status == "running":
        return out
    for entry in out.values():
        if not isinstance(entry, dict) or entry.get("state") in TERMINAL_STATES:
            continue
        if run_status == "failed":
            entry["state"] = "failed"
            entry["reason"] = entry.get("reason") or "Run ended"
            entry["error"] = entry.get("error") or run_error or "The run ended before this source finished"
        else:
            entry["state"] = "skipped"
            entry["reason"] = entry.get("reason") or "Run ended"
        entry["message"] = entry.get("message") or "Did not finish"
    # Progress keys: a source that finished cleanly has nothing left in its
    # pending batch and is past every phase (the snapshot may have been persisted
    # between its last tick and done()). Failed/skipped sources keep their last
    # funnel numbers as-is — the card shows the reason instead of the batch meter.
    for entry in out.values():
        if isinstance(entry, dict) and entry.get("state") == "done" and entry.get("phase") is not None:
            entry["phase"] = "done"
            if entry.get("in_batch") is not None:
                entry["in_batch"] = 0
    return out


class SourceStatusTracker:
    """Accumulates per-source state/count/message for one run (see module doc).

    Every mutator is async because it may emit a snapshot to ``on_change``.
    Unknown source keys are ignored (defensive: only enabled sources are tracked).
    ``on_change`` failures are logged and swallowed — status reporting must never
    break a scrape.
    """

    def __init__(
        self,
        sources: Iterable[str],
        on_change: OnChange | None = None,
        *,
        min_interval_s: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._entries: dict[str, dict[str, Any]] = initial_source_status(sources)
        self._on_change = on_change
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._last_emit: float | None = None
        self._dirty = False
        # Lazily created inside the running loop (the orchestrator may run on a
        # proactor worker thread with its own loop on Windows).
        self._lock: asyncio.Lock | None = None
        # At most one pending trailing flush for a throttled message change.
        self._flush_task: asyncio.Task | None = None
        self._flush_sleeping = False

    # ── Read ─────────────────────────────────────────────────────────────────

    @property
    def sources(self) -> list[str]:
        return list(self._entries)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Deep copy — safe to hand to another thread/loop for persistence."""
        return copy.deepcopy(self._entries)

    def state(self, source: str) -> str:
        entry = self._entries.get(source)
        return entry["state"] if entry else ""

    def count(self, source: str) -> int:
        entry = self._entries.get(source)
        return int(entry["count"]) if entry else 0

    # ── Mutators ─────────────────────────────────────────────────────────────

    async def start(self, source: str, message: str = "Starting…") -> None:
        if self._touch(source, state="running", message=message):
            await self._emit(force=True)

    async def message(self, source: str, msg: str) -> None:
        """Latest human-readable progress line for ONE source (throttled persist)."""
        if not msg:
            return
        if self._touch(source, message=msg):
            await self._emit(force=False)

    async def add_jobs(self, source: str, n: int, message: str | None = None) -> None:
        """A batch of ``n`` freshly-scraped jobs — bumps the live count (persisted
        immediately so the per-source counter visibly rises)."""
        entry = self._entries.get(source)
        if entry is None or n <= 0:
            return
        new_count = int(entry["count"]) + int(n)
        msg = message if message is not None else f"Scraping… {new_count} jobs so far"
        # A flush empties the pending batch: the batch meter clears and the
        # flushed-batch counter goes up by one.
        if self._touch(
            source, count=new_count, message=msg,
            in_batch=0, batches=int(entry.get("batches") or 0) + 1,
        ):
            await self._emit(force=True)

    async def progress(self, source: str, payload: dict[str, Any] | None) -> None:
        """Live-progress update from a source agent's ``on_progress`` callback.

        Unknown keys / bad values are ignored. If only ``done`` / ``in_batch``
        (one more job extracted) changed, the persist is throttled like a
        message; any other change (phase, found/listed/skipped/target, …) is
        emitted immediately. An optional ``message`` key rides along."""
        if not isinstance(payload, dict) or source not in self._entries:
            return
        fields = _clean_progress(payload)
        msg = payload.get("message")
        entry = self._entries[source]
        force = any(k not in TICK_KEYS and entry.get(k) != v for k, v in fields.items())
        if isinstance(msg, str) and msg:
            fields["message"] = msg
        if fields and self._touch(source, **fields):
            await self._emit(force=force)

    async def set_listing(self, source: str, message: str | None = None, **fields: Any) -> None:
        """Listing / phase totals (phase, reported_total, listed, skipped, target,
        cap, page, total_pages, batch_size, window_hours). Emitted immediately
        when any number changes."""
        payload: dict[str, Any] = dict(fields)
        if message:
            payload["message"] = message
        await self.progress(source, payload)

    async def tick(self, source: str, done: int, in_batch: int, message: str | None = None) -> None:
        """One more job extracted: ``done`` so far and ``in_batch`` waiting in the
        current persist batch. Throttled (≤ 1 write per ``min_interval_s``)."""
        payload: dict[str, Any] = {"done": done, "in_batch": in_batch}
        if message:
            payload["message"] = message
        await self.progress(source, payload)

    async def done(self, source: str, count: int | None = None, message: str | None = None) -> None:
        entry = self._entries.get(source)
        if entry is None:
            return
        final = int(entry["count"]) if count is None else int(count)
        msg = message if message is not None else f"Finished — {final} jobs scraped"
        extra: dict[str, Any] = {}
        if entry.get("phase") is not None:
            # Progress-reporting source: past every phase, nothing left pending.
            extra["phase"] = "done"
            if entry.get("in_batch") is not None:
                extra["in_batch"] = 0
        if self._touch(source, state="done", count=final, message=msg, **extra):
            await self._emit(force=True)

    async def fail(
        self,
        source: str,
        reason: str,
        error: str = "",
        count: int | None = None,
        message: str | None = None,
    ) -> None:
        entry = self._entries.get(source)
        if entry is None:
            return
        fields: dict[str, Any] = {
            "state": "failed",
            "reason": reason,
            "error": error or reason,
            "message": message if message is not None else f"Failed — {reason}",
        }
        if count is not None:
            fields["count"] = int(count)
        if self._touch(source, **fields):
            await self._emit(force=True)

    async def skip(self, source: str, reason: str, message: str | None = None) -> None:
        if self._touch(
            source, state="skipped", reason=reason,
            message=message if message is not None else f"Skipped — {reason}",
        ):
            await self._emit(force=True)

    def set_count(self, source: str, count: int) -> None:
        """Silent count reconcile (no emit) — used at the end of run_all to align
        each source's count with its final deduped/classified job total."""
        self._touch(source, count=int(count))

    async def flush(self) -> None:
        """Emit any throttled-but-unsent change now."""
        if self._dirty:
            await self._emit(force=True)

    async def aclose(self) -> None:
        """End of collection: cancel the trailing-flush timer (awaited, so no task
        outlives the run's loop) and flush anything still unsent. Idempotent."""
        task, self._flush_task = self._flush_task, None
        if task is not None and not task.done():
            # Only interrupt the timer while it is still sleeping — a flush that is
            # already mid-write is allowed to finish rather than being torn down.
            if self._flush_sleeping:
                task.cancel()
            # gather(return_exceptions) absorbs the timer's own CancelledError but
            # still propagates a cancellation of the caller itself.
            await asyncio.gather(task, return_exceptions=True)
        await self.flush()

    # ── Internal ─────────────────────────────────────────────────────────────

    def _touch(self, source: str, **fields: Any) -> bool:
        entry = self._entries.get(source)
        if entry is None:
            return False
        changed = False
        for key, value in fields.items():
            if entry.get(key) != value:
                entry[key] = value
                changed = True
        if changed:
            entry["updated_at"] = _now_iso()
            self._dirty = True
        return changed

    async def _emit(self, force: bool) -> None:
        if self._on_change is None:
            self._dirty = False
            return
        now = self._clock()
        if (
            not force
            and self._last_emit is not None
            and (now - self._last_emit) < self._min_interval_s
        ):
            # Throttled — stays dirty; a trailing flush sends it once the interval
            # elapses (unless a forced emit carries it first).
            self._schedule_trailing_flush(self._min_interval_s - (now - self._last_emit))
            return
        if self._lock is None:
            self._lock = asyncio.Lock()
        # Serialise writes and take the snapshot INSIDE the lock so the last
        # write to land always carries the newest state (no stale overwrite
        # when two sources emit concurrently).
        async with self._lock:
            self._last_emit = self._clock()
            self._dirty = False
            snap = self.snapshot()
            try:
                await self._on_change(snap)
            except Exception as exc:  # status reporting must never break a scrape
                logger.warning("source_status_emit_failed", error=str(exc))

    def _schedule_trailing_flush(self, delay: float) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return  # one pending timer is enough — it sends the latest state
        self._flush_sleeping = True
        self._flush_task = asyncio.ensure_future(self._trailing_flush(max(0.0, delay)))

    async def _trailing_flush(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        finally:
            self._flush_sleeping = False
        if self._dirty:
            await self._emit(force=True)
