"""
Manual re-enrichment + Apollo recruiter sweep — maintenance / backlog tool.
============================================================================

Why this exists
───────────────
Jobs that degraded to selector-only during a harvest (every LLM provider was
down) are queued in `reenrichment_tasks`. They're normally retried only at the
START of a background harvest — but that background task is scheduled *after* the
daily-cap gate, so once today's total hits MAX_JOBS_PER_DAY the harvest POST is
rejected (HTTP 429) and the backlog is never drained. This script runs the same
work directly, so it is UNAFFECTED by the daily cap (it never touches run_guard).

What it does (two phases, in order)
───────────────────────────────────
    Phase 1 — run_reenrichment_sweep(): replay each pending task's stored payload
              through the LLM (primary + configured fallback, via
              LLMService.extract_json), backfill the scraped_jobs row, and upsert
              recruiter identity. Drains the WHOLE backlog by default and retries
              aged tasks instead of expiring them (override with --max-age-days).
    Phase 2 — run_apollo_recruiter_sweep(): every recruiter that now has a
              LinkedIn URL but still no email is sent to Apollo (by LinkedIn URL)
              for email/phone; the result is written to the recruiters table.
              Apollo's own recheck cooldown (APOLLO_RECHECK_DAYS) prevents
              re-billing a profile tried recently.

Usage
─────
    # Full workflow (drain all tasks, then Apollo-enrich all missing-email recruiters):
    python scripts/reenrich_and_apollo.py

    # Only re-extract jobs (no Apollo credits spent):
    python scripts/reenrich_and_apollo.py --skip-apollo

    # Only run the Apollo phase against already-known recruiters:
    python scripts/reenrich_and_apollo.py --skip-reenrich

    # Cap work / credits, and see per-task debug logs:
    python scripts/reenrich_and_apollo.py --reenrich-limit 100 --apollo-limit 50 --verbose

Runs against whatever DATABASE_URL / .env resolves to (the same DB the app uses) —
inside the container: `docker exec -it <api> python scripts/reenrich_and_apollo.py`.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow `python scripts/reenrich_and_apollo.py` from the project root (scripts/ is
# added to sys.path[0], not the project root, so `app` wouldn't otherwise import).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.logging_config import configure_logging
from app.models.recruiter import RecruiterORM  # noqa: F401  — register the mapper so ScrapedJobORM.recruiter resolves
from app.services.recruiter_apollo_sweep import run_apollo_recruiter_sweep
from app.services.reenrichment_service import run_reenrichment_sweep

# "drain everything" defaults — large enough to cover any realistic backlog while
# still being an explicit, overridable number rather than an unbounded query.
_ALL = 1_000_000


async def _run(args: argparse.Namespace) -> None:
    if not args.skip_reenrich:
        print("== Phase 1: re-enrichment sweep (LLM re-extraction) ==")
        sweep = await run_reenrichment_sweep(limit=args.reenrich_limit, max_age_days=args.max_age_days)
        print(
            f"  pending={sweep['pending']}  done={sweep['done']}  "
            f"expired={sweep['expired']}  still_pending={sweep['still_pending']}"
        )
        if sweep["still_pending"] and not sweep["done"]:
            print("  note: tasks left pending -- the LLM may still be unavailable. Re-run later.")
    else:
        print("== Phase 1: skipped (--skip-reenrich) ==")

    if not args.skip_apollo:
        print("== Phase 2: Apollo recruiter enrichment (email by LinkedIn URL) ==")
        apollo = await run_apollo_recruiter_sweep(limit=args.apollo_limit)
        print(
            f"  candidates={apollo['candidates']}  attempted={apollo['attempted']}  "
            f"emails_found={apollo['emails_found']}  skipped={apollo['skipped']}"
        )
        if apollo["candidates"] and not apollo["attempted"]:
            print("  note: nothing attempted -- check APOLLO_API_KEY, or all candidates are in cooldown.")
    else:
        print("== Phase 2: skipped (--skip-apollo) ==")

    print("Done.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Drain the re-enrichment backlog via the LLM, then Apollo-enrich the resulting recruiters."
    )
    parser.add_argument(
        "--reenrich-limit", type=int, default=_ALL,
        help="Max pending re-enrichment tasks to process (default: all).",
    )
    parser.add_argument(
        "--apollo-limit", type=int, default=_ALL,
        help="Max recruiters to send to Apollo (default: all missing-email recruiters).",
    )
    parser.add_argument(
        "--max-age-days", type=int, default=_ALL,
        help="Retry tasks up to this age in days; older ones are marked expired "
             "(default: effectively unlimited, so nothing is expired).",
    )
    parser.add_argument("--skip-reenrich", action="store_true", help="Skip phase 1 (LLM re-extraction).")
    parser.add_argument("--skip-apollo", action="store_true", help="Skip phase 2 (Apollo enrichment).")
    parser.add_argument("--verbose", action="store_true", help="Show per-task DEBUG logs.")
    args = parser.parse_args()

    configure_logging("DEBUG" if args.verbose else "INFO")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
