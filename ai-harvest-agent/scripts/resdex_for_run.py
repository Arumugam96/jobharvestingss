"""Re-run the post-harvest Resdex step for an EXISTING harvest run — exactly what
the harvest does at the end (run_auto_outreach_after_harvest), so the same
settings apply (RESDEX_*, OUTREACH_AUTO_SEND_ON_HARVEST).

Use it when the Resdex step was skipped or failed (e.g. the recruiter session had
expired) — re-Connect, then:

  python scripts/resdex_for_run.py                   # newest run
  python scripts/resdex_for_run.py 20261015_064220   # a specific run_id

Sends email ONLY if OUTREACH_AUTO_SEND_ON_HARVEST=true; pass --no-send to force a
search-and-store-only pass regardless.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import desc, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core.tenant_context import set_current_tenant  # noqa: E402
from app.models.harvest_run import HarvestRunORM  # noqa: E402
from app.services.auto_outreach_service import run_auto_outreach_after_harvest  # noqa: E402
from app.services.harvest_run_service import HarvestRunService, db_read  # noqa: E402


async def main(a: argparse.Namespace) -> None:
    settings = get_settings()
    if a.no_send:
        settings.outreach_auto_send_on_harvest = False   # this process only
    set_current_tenant("__all__")

    async def _run(db):
        stmt = select(HarvestRunORM)
        stmt = stmt.where(HarvestRunORM.run_id == a.run_id) if a.run_id else stmt
        return (await db.execute(stmt.order_by(desc(HarvestRunORM.created_at)).limit(1))).scalar()

    run = await db_read(_run)
    if run is None:
        print("run not found")
        return
    rows = await db_read(lambda db: HarvestRunService(db).list_jobs_for_run(run.id)) or []
    set_current_tenant(run.tenant_id)
    print(f"run {run.run_id} tenant={run.tenant_id} jobs={len(rows)} "
          f"auto_send={settings.outreach_auto_send_on_harvest} resdex={settings.resdex_enabled}")
    result = await run_auto_outreach_after_harvest(rows, run_id=run.run_id)
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id", nargs="?", default=None)
    ap.add_argument("--no-send", action="store_true")
    asyncio.run(main(ap.parse_args()))
