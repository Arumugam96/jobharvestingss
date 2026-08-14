"""
Delete a harvest run and all its child rows — maintenance / cleanup tool.
==========================================================================

Why this exists
───────────────
harvest.db has no ON DELETE CASCADE and the SQLite FK pragma is off, so
deleting a run by hand means deleting its children first, in the right order,
or you silently orphan rows. This script does it safely in one transaction.

What it deletes (child-first)
─────────────────────────────
    1. llm_calls     WHERE run_id = <harvest_runs.id>
    2. scraped_jobs  WHERE run_id = <harvest_runs.id>
    3. harvest_runs  WHERE id     = <harvest_runs.id>

It does NOT touch `recruiters` — those are shared identity rows referenced by
jobs across many runs.

Identifier note
───────────────
`scraped_jobs.run_id` / `llm_calls.run_id` are foreign keys to
`harvest_runs.id` (the surrogate UUID PK), NOT the human `run_id` string
column. You may pass EITHER here — pass the UUID with --id, or the human
run_id string with --run-id and the script resolves it to the UUID first.

Usage
─────
    # List recent runs so you can pick the bad one:
    python scripts/delete_run.py --list

    # Dry run (default) — shows what WOULD be deleted, changes nothing:
    python scripts/delete_run.py --run-id 20260813_101500_java_pune

    # Actually delete (child-first, single transaction):
    python scripts/delete_run.py --run-id 20260813_101500_java_pune --yes
    python scripts/delete_run.py --id 4b1e...-uuid --yes

Runs against whatever DATABASE_URL resolves to (the same DB the app uses) —
inside the container: `docker exec -it <api> python scripts/delete_run.py ...`.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow `python scripts/delete_run.py` from the project root (scripts/ is added
# to sys.path[0], not the project root, so `app` wouldn't otherwise import).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, select
from sqlalchemy.orm import noload

from app.config import get_settings
from app.core.dependencies import get_session_factory
from app.models.harvest_run import HarvestRunORM, LlmCallORM, ScrapedJobORM
from app.models.recruiter import RecruiterORM  # noqa: F401  — register the mapper so ScrapedJobORM.recruiter resolves


async def _list_runs() -> None:
    factory = get_session_factory(get_settings())
    async with factory() as db:
        rows = (
            await db.execute(
                select(HarvestRunORM)
                .options(noload(HarvestRunORM.jobs))  # don't touch scraped_jobs (avoids loading child rows)
                .order_by(HarvestRunORM.created_at.desc())
                .limit(30)
            )
        ).scalars().all()
    if not rows:
        print("No harvest runs found.")
        return
    print(f"{'id (uuid)':38}  {'run_id':28}  {'status':10}  {'jobs':>5}  created_at")
    print("-" * 110)
    for r in rows:
        print(
            f"{r.id:38}  {(r.run_id or ''):28.28}  {(r.status or ''):10}  "
            f"{r.combined_count:>5}  {r.created_at}"
        )


async def _resolve_pk(db, *, run_pk: str | None, run_id: str | None) -> str | None:
    """Return the harvest_runs.id (UUID PK) for the given --id or --run-id."""
    if run_pk:
        row = (
            await db.execute(
                select(HarvestRunORM)
                .options(noload(HarvestRunORM.jobs))
                .where(HarvestRunORM.id == run_pk)
            )
        ).scalar_one_or_none()
        return row.id if row else None
    # --run-id: human string column; may match more than one row (rare) — take newest.
    row = (
        await db.execute(
            select(HarvestRunORM)
            .options(noload(HarvestRunORM.jobs))
            .where(HarvestRunORM.run_id == run_id)
            .order_by(HarvestRunORM.created_at.desc())
        )
    ).scalars().first()
    return row.id if row else None


async def _delete_run(*, run_pk: str | None, run_id: str | None, commit: bool) -> None:
    factory = get_session_factory(get_settings())
    async with factory() as db:
        pk = await _resolve_pk(db, run_pk=run_pk, run_id=run_id)
        if pk is None:
            print(f"No harvest run found for {'--id ' + run_pk if run_pk else '--run-id ' + str(run_id)}.")
            return

        run = (
            await db.execute(
                select(HarvestRunORM).options(noload(HarvestRunORM.jobs)).where(HarvestRunORM.id == pk)
            )
        ).scalar_one()
        n_jobs = (
            await db.execute(
                select(func.count()).select_from(ScrapedJobORM).where(ScrapedJobORM.run_id == pk)
            )
        ).scalar_one()
        n_calls = (
            await db.execute(
                select(func.count()).select_from(LlmCallORM).where(LlmCallORM.run_id == pk)
            )
        ).scalar_one()

        print(
            f"Run {pk}\n"
            f"  run_id={run.run_id!r}  status={run.status!r}  created_at={run.created_at}\n"
            f"  will delete: {n_calls} llm_calls, {n_jobs} scraped_jobs, 1 harvest_runs"
        )

        if not commit:
            print("\nDRY RUN -- nothing deleted. Re-run with --yes to apply.")
            return

        # Child-first, single transaction.
        await db.execute(delete(LlmCallORM).where(LlmCallORM.run_id == pk))
        await db.execute(delete(ScrapedJobORM).where(ScrapedJobORM.run_id == pk))
        await db.execute(delete(HarvestRunORM).where(HarvestRunORM.id == pk))
        await db.commit()
        print("\nDeleted.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete a harvest run and its child rows (child-first).")
    parser.add_argument("--list", action="store_true", help="List recent runs and exit.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--id", dest="run_pk", help="harvest_runs.id (UUID PK).")
    group.add_argument("--run-id", dest="run_id", help="human run_id string column.")
    parser.add_argument("--yes", action="store_true", help="Actually delete (default is a dry run).")
    args = parser.parse_args()

    if args.list:
        asyncio.run(_list_runs())
        return
    if not args.run_pk and not args.run_id:
        parser.error("provide --id or --run-id (or --list).")
    asyncio.run(_delete_run(run_pk=args.run_pk, run_id=args.run_id, commit=args.yes))


if __name__ == "__main__":
    main()
