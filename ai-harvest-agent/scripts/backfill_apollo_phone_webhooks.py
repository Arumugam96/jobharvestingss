"""
Backfill Apollo async phone deliveries that matched no request row.

Before refs were tracked for harvest enrichment, Apollo's phone webhook deliveries
arrived with an empty ref and no apollo_phone_requests row to match — the numbers
exist only as raw JSON in apollo_webhook_events. This re-parses those events and
lands each delivered phone where it belongs (apollo_phone_service.reprocess_events):

  * an open (pending / expired / rejected) request for the Apollo person id, else
  * a recruiter / finder row identified by EXACT ids only — recruiters.apollo_person_id,
    recruiter_finder_items.apollo_person_id / reveal_log.person_id, or the LinkedIn URL
    in the body (opt-in: --resolve-via-apollo looks the URL up at Apollo, capped by
    --max-resolve; credit cost unverified). Never matched by timestamp proximity.
  * Unresolved phones are still stored (FOUND request row, no links) and listed.

Charges (default on; --no-record-charges skips): purpose "enrichment" — the global
counter + enrichment purpose counter for the EVENT's UTC date, and a ledger row dated
at the event's received_at. The per-tenant Finder budget is never touched.

Idempotent: deterministic refs per (event, person), a FOUND row for the person within
a day of the event is skipped, and processed events get reprocessed_at (excluded on the
next run unless --event-id / --include-reprocessed). Numbers are MASKED in the report.

Usage (inside the api container):
    python scripts/backfill_apollo_phone_webhooks.py                 # dry run (default)
    python scripts/backfill_apollo_phone_webhooks.py --apply
    python scripts/backfill_apollo_phone_webhooks.py --apply --since 2026-11-01
    python scripts/backfill_apollo_phone_webhooks.py --apply --event-id <id> --event-id <id>
    python scripts/backfill_apollo_phone_webhooks.py --apply --resolve-via-apollo --max-resolve 5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models.apollo_phone  # noqa: F401,E402 — register mappers
import app.models.harvest_run  # noqa: F401,E402
import app.models.recruiter  # noqa: F401,E402
import app.models.recruiter_finder  # noqa: F401,E402
from app.core.logging_config import configure_logging  # noqa: E402
from app.services.apollo_phone_service import reprocess_events  # noqa: E402


def _since(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false", default=False,
                      help="Report only; every write is rolled back (default).")
    mode.add_argument("--apply", dest="apply", action="store_true", help="Commit the backfill.")
    p.add_argument("--since", type=_since, default=None,
                   help="Only events received at/after this ISO date/time (UTC if no tz).")
    p.add_argument("--event-id", dest="event_ids", action="append", default=None,
                   help="Reprocess only this event id (repeatable; includes already-reprocessed).")
    p.add_argument("--include-reprocessed", action="store_true",
                   help="Also revisit events a previous backfill already processed.")
    p.add_argument("--no-record-charges", dest="record_charges", action="store_false", default=True,
                   help="Don't book the 8-credit phone charges on the counters/ledger.")
    p.add_argument("--resolve-via-apollo", action="store_true",
                   help="Look unresolved person ids up at Apollo (no reveal) for a LinkedIn URL.")
    p.add_argument("--max-resolve", type=int, default=25, help="Cap on --resolve-via-apollo lookups.")
    p.add_argument("--json", action="store_true", help="Print the full JSON report.")
    return p.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    report = await reprocess_events(
        dry_run=not args.apply, since=args.since, event_ids=args.event_ids,
        record_charges=args.record_charges, resolve_via_apollo=args.resolve_via_apollo,
        max_resolve=args.max_resolve, include_reprocessed=args.include_reprocessed,
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 1 if report["errors"] else 0

    mode = "APPLY" if args.apply else "DRY RUN (nothing written — pass --apply to commit)"
    print(f"== Apollo phone webhook backfill: {mode} ==")
    for ev in report["events"]:
        print(f"\nevent {ev['event_id']}  received {ev['received_at']}"
              f"  applied={ev['applied']} found={ev['found']} charged={ev['charged']}")
        if ev["error"]:
            print(f"  ! {ev['error']}")
        for p in ev["people"]:
            print(f"  - person {p['person_id'] or '(no id)'}: {p['outcome']}"
                  f"{'  via ' + p['via'] if p['via'] else ''}"
                  f"{'  phone ' + p['phone'] + ' (' + p['phone_status'] + ')' if p['phone'] else ''}"
                  f"{'  recruiter ' + p['recruiter_id'] if p.get('recruiter_id') else ''}"
                  f"{'  tenant ' + p['tenant_id'] if p.get('tenant_id') else ''}"
                  f"{'  charged ' + str(p['charged']) if p['charged'] else ''}")
    t = report["totals"]
    print(f"\nTotals: events={t['events']} people={t['people']} applied={t['applied']} "
          f"found={t['found']} credits_booked={t['charged']} unlinked={t['unlinked']}")
    print(f"Apollo lookups: {report['apollo_lookups']}")
    if report["unresolved"]:
        print("\nUnresolved phones (stored on unlinked request rows):")
        for u in report["unresolved"]:
            print(f"  event {u['event_id']} person {u['person_id']} phone {u['phone']}")
    if report["errors"]:
        print("\nErrors:")
        for e in report["errors"]:
            print(f"  event {e['event_id']}: {e['error']}")
    return 1 if report["errors"] else 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    return asyncio.run(_run(_parse(argv)))


if __name__ == "__main__":
    sys.exit(main())
