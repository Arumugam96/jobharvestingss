"""
Job Tracker — process-level in-memory status store for background harvest runs.

Every POST /run-harvest-agent creates one entry here.
The background coroutine updates it at key milestones.
GET /harvest-status/{job_id} reads from it.

State is persisted to disk after every write so the frontend can poll
even after a hot-reload (though in-flight jobs are marked "failed" on
restart because their background tasks don't survive process termination).
Only the newest _MAX_JOBS entries are kept (running ones are never dropped),
and the file is replaced atomically so a crash mid-write can't truncate it.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_TRACKER_FILE = Path("data/results/run_history/job_tracker.json")

# Cap on retained entries (memory and file size). The DB run row is the durable
# status record; this store only backs recent polling / the JSON fallback.
_MAX_JOBS = 200


@dataclass
class JobStatus:
    job_id:       str
    run_id:       str = ""
    status:       str = "running"   # running | success | no_results | failed
    progress:     int = 0           # 0–100
    message:      str = "Harvest started"
    linkedin:     int = 0
    naukri:       int = 0
    dice:         int = 0
    combined:     int = 0
    started_at:   str = ""
    completed_at: str = ""
    # we don't need these two path's
    # excel_path:   str = ""
    # json_path:    str = ""
    error:        str = ""
    token_usage:  dict = field(default_factory=dict)
    # Per-source live status ({source: {state, count, message, reason, error,
    # updated_at}}) — see app/services/source_status.py. {} for single-source runs.
    sources:      dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialise the status as a plain dict (the shape persisted and polled by the UI)."""
        return asdict(self)


class JobTracker:
    """Process-level keyed store — no external dependencies, no locks needed
    because FastAPI runs a single-threaded async event loop."""

    _jobs: dict[str, JobStatus] = {}

    # ── Write ─────────────────────────────────────────────────────────────────

    @classmethod
    def create(cls, job_id: str, run_id: str) -> JobStatus:
        """Register a new ``running`` entry stamped with the current UTC time and persist it."""
        js = JobStatus(
            job_id     = job_id,
            run_id     = run_id,
            started_at = datetime.now(timezone.utc).isoformat(),
        )
        cls._jobs[job_id] = js
        cls._prune()
        cls._persist()
        return js

    @classmethod
    def update(cls, job_id: str, **kwargs: Any) -> None:
        """Set the given fields on a tracked job and persist.

        Unknown job ids are ignored, as are keyword names that are not JobStatus fields.
        """
        js = cls._jobs.get(job_id)
        if js is None:
            return
        for k, v in kwargs.items():
            if hasattr(js, k):
                setattr(js, k, v)
        cls._persist()

    # ── Read ──────────────────────────────────────────────────────────────────

    @classmethod
    def get(cls, job_id: str) -> JobStatus | None:
        """Return the in-memory status for ``job_id``, or None if it is not tracked."""
        return cls._jobs.get(job_id)

    # ── Startup restore ───────────────────────────────────────────────────────

    @classmethod
    def load_from_disk(cls) -> None:
        """
        Called once at application startup.
        Any job still in 'running' state didn't survive the restart — mark failed.
        Only the newest _MAX_JOBS entries are kept.
        """
        try:
            if not _TRACKER_FILE.exists():
                return
            data = json.loads(_TRACKER_FILE.read_text(encoding="utf-8"))
            for job_id, d in data.items():
                js = JobStatus(**{k: v for k, v in d.items() if k in JobStatus.__dataclass_fields__})
                if js.status == "running":
                    js.status  = "failed"
                    js.message = "Server restarted during harvest"
                    js.error   = "Server restarted during harvest"
                cls._jobs[job_id] = js
            cls._prune()
        except Exception:
            pass

    # ── Internal ──────────────────────────────────────────────────────────────

    @classmethod
    def _prune(cls) -> None:
        """Drop the oldest finished entries (by started_at) beyond _MAX_JOBS.

        Running entries are never dropped, so a live run's status stays pollable."""
        if len(cls._jobs) <= _MAX_JOBS:
            return
        finished = sorted(
            (js for js in cls._jobs.values() if js.status != "running"),
            key=lambda js: js.started_at or "",
        )
        excess = len(cls._jobs) - _MAX_JOBS
        for js in finished[:excess]:
            cls._jobs.pop(js.job_id, None)

    @classmethod
    def _persist(cls) -> None:
        """Atomically rewrite the tracker JSON file with every tracked job (a unique
        temp file + os.replace, so a crash or a concurrent writer never leaves it
        truncated); any I/O error is silently ignored."""
        tmp = None
        try:
            _TRACKER_FILE.parent.mkdir(parents=True, exist_ok=True)
            # list() snapshots the entries: update() can run on the proactor thread too.
            payload = json.dumps({k: v.to_dict() for k, v in list(cls._jobs.items())}, indent=2)
            fd, tmp = tempfile.mkstemp(
                dir=_TRACKER_FILE.parent, prefix=_TRACKER_FILE.name + ".", suffix=".tmp",
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, _TRACKER_FILE)
            tmp = None
        except Exception:
            pass
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
