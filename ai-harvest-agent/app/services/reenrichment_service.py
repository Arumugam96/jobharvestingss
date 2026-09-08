"""Start-of-run re-enrichment sweep.

When a harvest degrades because every LLM provider (local + configured fallback)
was down, the affected jobs are saved with selector-only data and queued in
`reenrichment_tasks` (see harvest_run_service + linkedin_agent). This module
retries those tasks at the START of each harvest run: it replays the stored
extraction payload through LLMService.extract_json — which now fails over on its
own — and backfills the scraped_jobs row on success. Tasks older than
settings.reenrichment_max_age_days are given up on (marked failed).

Kept separate from the harvest flow so it can be reused (e.g. a manual endpoint
or a scheduled task) later without dragging in the orchestrator.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from app.config import get_settings
from app.core.exceptions import LLMUnavailableError
from app.services.harvest_run_service import HarvestRunService, db_read, db_write
from app.services.llm_service import LLMService

logger = structlog.get_logger(__name__)


async def run_reenrichment_sweep(
    limit: int | None = None,
    max_age_days: int | None = None,
) -> dict[str, int]:
    """Retry pending re-enrichment tasks (oldest first, capped). Returns a summary
    dict {pending, done, expired, still_pending}. Never raises — a failure here
    must not block the harvest that triggered it.

    `limit` and `max_age_days` default to settings.reenrichment_sweep_limit /
    settings.reenrichment_max_age_days (the start-of-harvest behavior). A manual
    caller (scripts/reenrich_and_apollo.py) passes large values to drain the whole
    backlog and to retry aged tasks instead of expiring them."""
    settings = get_settings()
    lim = limit if limit is not None else settings.reenrichment_sweep_limit
    age_days = max_age_days if max_age_days is not None else settings.reenrichment_max_age_days
    summary = {"pending": 0, "done": 0, "expired": 0, "still_pending": 0}
    try:
        tasks = await db_read(
            lambda db: HarvestRunService(db).list_pending_reenrichment(lim)
        ) or []
    except Exception as exc:  # defensive — db_read already swallows, but never raise upward
        logger.warning("reenrichment_sweep_list_failed", error=str(exc))
        return summary

    if not tasks:
        return summary
    summary["pending"] = len(tasks)

    llm = LLMService(settings)
    max_age = timedelta(days=age_days)
    now = datetime.now(timezone.utc)
    logger.debug("reenrichment_sweep_start", pending=len(tasks))

    for t in tasks:
        logger.debug(
            "reenrichment_task_started",
            run_id=t["run_id"],
            job_url=t["job_url"],
        )
        first_seen = t.get("first_seen_at")
        if first_seen is not None:
            # server_default now() is tz-aware on Postgres but may be naive on SQLite.
            if first_seen.tzinfo is None:
                first_seen = first_seen.replace(tzinfo=timezone.utc)
            if now - first_seen > max_age:
                await db_write(lambda db: HarvestRunService(db).fail_reenrichment(
                    t["id"], t["run_id"], t["job_url"], "expired"
                ))
                summary["expired"] += 1
                continue

        try:
            extracted = await llm.extract_json(
                content=t["content"],
                schema_description=t["schema_description"],
                system=t["system"],
                job_url=t["job_url"],
            )
        except LLMUnavailableError:
            # Every provider is still down — record the attempt and STOP the sweep;
            # the remaining tasks stay pending for a later run (don't burn the
            # harvest's start-up waiting on a dead LLM for each one).
            await db_write(lambda db: HarvestRunService(db).bump_reenrichment_attempt(t["id"]))
            summary["still_pending"] += 1
            logger.info("reenrichment_sweep_llm_still_down", processed=summary)
            break
        except Exception as exc:
            # Content/parse error for this one — record the attempt, keep going.
            await db_write(lambda db: HarvestRunService(db).bump_reenrichment_attempt(t["id"]))
            summary["still_pending"] += 1
            logger.warning("reenrichment_task_error", task_id=t["id"], error=str(exc))
            continue

        await db_write(lambda db: HarvestRunService(db).apply_reenrichment(
            t["id"], t["run_id"], t["job_url"], extracted
        ))
        summary["done"] += 1

    logger.info("reenrichment_sweep_done", **summary)
    return summary
