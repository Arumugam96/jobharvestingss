"""Local JSON backup of a harvest's final job list.

The database is the system of record, but the end-of-run save can fail (DB/tunnel
down, timeout, a row Postgres rejects) — and the run no longer writes any other
result file, so without this the jobs would exist only in memory and be gone
once the run ends.

One file (settings.harvest_backup_path), REPLACED by every harvest:

* written BEFORE the DB save with ``db_saved: null`` — so a crash mid-save still
  leaves the jobs on disk;
* rewritten after the save with ``db_saved: true / false``.

Safety: if the backup about to be replaced never reached the DB (``db_saved`` is
not true — the save failed, or the process died mid-save), it is moved aside to
``<name>_unsaved_<run_id>.json`` rather than overwritten, so unsaved jobs are
never silently lost. Writes are atomic (temp file + os.replace) and never raise.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def backup_path() -> Path:
    from app.config import get_settings

    return Path(get_settings().harvest_backup_path)


def _read_header(path: Path) -> dict[str, Any] | None:
    """The existing backup's metadata (everything but the job list), or None."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if k != "jobs"} if isinstance(data, dict) else None
    except Exception:
        return None


def _write_sync(
    *,
    run_id: str,
    run_pk: str | None,
    tenant_id: str | None,
    status: str,
    jobs: list[dict[str, Any]],
    db_saved: bool | None,
) -> Path | None:
    path = backup_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        # Never overwrite a DIFFERENT run's backup whose jobs never reached the DB.
        if path.exists():
            prev = _read_header(path)
            unsaved = prev is not None and prev.get("db_saved") is not True
            if unsaved and prev.get("run_id") != run_id:
                prev_run = prev.get("run_id") or "unknown"
                aside = path.with_name(f"{path.stem}_unsaved_{prev_run}{path.suffix}")
                os.replace(path, aside)
                logger.warning(
                    "harvest_backup_previous_unsaved_kept",
                    previous_run_id=prev.get("run_id"), kept_as=str(aside),
                    jobs=prev.get("job_count"),
                    note="previous harvest's jobs never reached the DB — kept, not overwritten",
                )

        payload = {
            "run_id":     run_id,
            "run_pk":     run_pk,
            "tenant_id":  tenant_id,
            "status":     status,
            "db_saved":   db_saved,
            "written_at": datetime.now(timezone.utc).isoformat(),
            "job_count":  len(jobs),
            "jobs":       jobs,
        }
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, path)  # atomic: a crash mid-write never corrupts the last backup
        logger.info("harvest_backup_written", path=str(path.resolve()), run_id=run_id,
                    jobs=len(jobs), db_saved=db_saved)
        return path
    except Exception as exc:
        logger.warning("harvest_backup_write_failed", path=str(path), run_id=run_id, error=str(exc))
        return None


async def write_harvest_backup(
    *,
    run_id: str,
    run_pk: str | None,
    status: str,
    jobs: list[dict[str, Any]],
    db_saved: bool | None = None,
) -> Path | None:
    """Write (replace) the local backup of this run's final job list. Runs the
    JSON encode/write in a worker thread (large runs are many MB). Never raises."""
    try:
        from app.core.tenant_context import get_current_tenant_id

        tenant_id = get_current_tenant_id()
    except Exception:
        tenant_id = None
    try:
        return await asyncio.to_thread(
            _write_sync, run_id=run_id, run_pk=run_pk, tenant_id=tenant_id,
            status=status, jobs=jobs, db_saved=db_saved,
        )
    except Exception as exc:
        logger.warning("harvest_backup_write_failed", run_id=run_id, error=str(exc))
        return None
