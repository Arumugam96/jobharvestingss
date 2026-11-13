"""Where did today's Apollo credits go, and what did they buy?

Read-only. Compares three things for a day (IST, the same day boundary the harvest
report uses):

  1. APOLLO'S OWN COUNTER  — apollo_credit_snapshots (15-min readings of Apollo's
     lead_credit pool) + one live reading (credit_usage_stats costs 0 credits).
     This is the number you see in the Apollo dashboard.
  2. WHAT THE APP RECORDED — apollo_credit_ledger, by purpose x endpoint and by hour.
     The ledger only books what a response *visibly* returned (email revealed = 1,
     phone delivered = 8, org returned = 1). Apollo-UI exports, other users of the
     key, and charges for matches that came back WITHOUT an email never reach it.
  3. WHAT THE CREDITS BOUGHT — emails/phones/orgs that actually landed in the DB.

  GAP (1 - 2) per hour = credits spent somewhere the app could not see.

Usage (inside the api container, or anywhere DATABASE_URL reaches Postgres):
    cd ai-harvest-agent
    python scripts/apollo_credit_audit.py                 # today (IST)
    python scripts/apollo_credit_audit.py --date 2026-11-12
    python scripts/apollo_credit_audit.py --no-live       # skip the live Apollo reading
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

IST = timezone(timedelta(hours=5, minutes=30), "IST")


def _hdr(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def _ist_hour(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(IST).strftime("%H:00")


async def _q(db, sql: str, params: dict) -> list:
    """Run one query; a missing table/column prints a note instead of aborting."""
    from sqlalchemy import text

    try:
        return list((await db.execute(text(sql), params)).mappings().all())
    except Exception as exc:  # noqa: BLE001 — diagnostic script, keep going
        await db.rollback()
        print(f"  (query skipped: {str(exc).splitlines()[0][:150]})")
        return []


async def main(day: date, live: bool) -> None:
    from app.config import get_settings
    from app.core.dependencies import get_session_factory

    start = datetime(day.year, day.month, day.day, tzinfo=IST).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    p = {"s": start, "e": end}
    settings = get_settings()

    print(f"Apollo credit audit for {day} IST  ({start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} UTC)")
    print(f"caps: APOLLO_DAILY_CAP={settings.apollo_daily_cap}  "
          f"APOLLO_ENRICHMENT_DAILY_CAP={settings.apollo_enrichment_daily_cap}  "
          f"apollo_reveal_phone={getattr(settings, 'apollo_reveal_phone', '?')}  "
          f"webhook={'set' if getattr(settings, 'apollo_webhook_url', '') else 'unset'}")

    factory = get_session_factory(settings)
    async with factory() as db:
        # ── 1. Apollo's own counter ──────────────────────────────────────────
        _hdr("1. APOLLO'S OWN COUNTER (apollo_credit_snapshots)")
        snaps = await _q(db, """
            (SELECT taken_at, consumed, cycle_start FROM apollo_credit_snapshots
              WHERE taken_at <= :s ORDER BY taken_at DESC LIMIT 1)
            UNION ALL
            (SELECT taken_at, consumed, cycle_start FROM apollo_credit_snapshots
              WHERE taken_at > :s AND taken_at <= :e ORDER BY taken_at)
            ORDER BY taken_at
        """, p)
        if live and end > datetime.now(timezone.utc):
            from app.services.apollo_credit_usage import fetch_credit_usage

            now = await fetch_credit_usage(settings)
            if now:
                snaps.append({"taken_at": datetime.now(timezone.utc),
                              "consumed": now["consumed"], "cycle_start": now["cycle_start"]})
                print(f"  live reading: {now['consumed']} / {now['limit']} used this cycle "
                      f"({now['left_over']} left)")
            else:
                print("  live reading unavailable (no key / not a master key / API error)")

        apollo_by_hour: dict[str, int] = defaultdict(int)
        for prev, cur in zip(snaps, snaps[1:]):
            same_cycle = prev["cycle_start"] == cur["cycle_start"]
            delta = (cur["consumed"] - prev["consumed"]) if same_cycle else cur["consumed"]
            apollo_by_hour[_ist_hour(cur["taken_at"])] += max(0, delta)
        apollo_total = sum(apollo_by_hour.values())
        if len(snaps) < 2:
            print("  not enough snapshots for this day — is the 15-min snapshot job running?")
        else:
            print(f"  Apollo charged {apollo_total} credits "
                  f"({len(snaps)} readings, {snaps[0]['taken_at']:%H:%M}..{snaps[-1]['taken_at']:%H:%M} UTC)")

        # ── 2. What the app recorded ─────────────────────────────────────────
        _hdr("2. WHAT THE APP RECORDED (apollo_credit_ledger)")
        led = await _q(db, """
            SELECT purpose, endpoint, COUNT(*) AS calls, SUM(credits) AS credits
              FROM apollo_credit_ledger WHERE created_at >= :s AND created_at < :e
             GROUP BY purpose, endpoint ORDER BY SUM(credits) DESC
        """, p)
        ledger_total = sum(int(r["credits"] or 0) for r in led)
        for r in led:
            print(f"  {r['purpose'] or '-':<12} {r['endpoint']:<28} "
                  f"{r['calls']:>5} billed calls  {r['credits']:>5} credits")
        print(f"  TOTAL recorded by app: {ledger_total}")

        led_hour = await _q(db, """
            SELECT created_at, credits FROM apollo_credit_ledger
             WHERE created_at >= :s AND created_at < :e
        """, p)
        ledger_by_hour: dict[str, int] = defaultdict(int)
        for r in led_hour:
            ledger_by_hour[_ist_hour(r["created_at"])] += int(r["credits"] or 0)

        # ── Gap by hour ──────────────────────────────────────────────────────
        _hdr("GAP BY HOUR (IST)  — Apollo charged vs app recorded")
        print(f"  {'hour':<6} {'apollo':>7} {'app':>6} {'gap':>6}")
        for h in sorted(set(apollo_by_hour) | set(ledger_by_hour)):
            a, l_ = apollo_by_hour.get(h, 0), ledger_by_hour.get(h, 0)
            flag = "   <-- unseen spend" if a - l_ >= 10 else ""
            print(f"  {h:<6} {a:>7} {l_:>6} {a - l_:>6}{flag}")
        if apollo_total:
            gap = apollo_total - ledger_total
            print(f"\n  unseen by app: {gap} of {apollo_total} credits "
                  f"({100 * gap / apollo_total:.0f}%)")

        # ── 3. What the credits bought ───────────────────────────────────────
        _hdr("3. WHAT THE CREDITS BOUGHT")

        print("\n  Harvest/maintenance person reveals (recruiters.apollo_enriched_at):")
        for r in await _q(db, """
            SELECT harvest_source,
                   COUNT(*) AS attempted,
                   COUNT(*) FILTER (WHERE enrichment_source = 'apollo'
                                      AND COALESCE(official_email_id,'') <> '') AS apollo_email,
                   COUNT(*) FILTER (WHERE enrichment_source = 'apollo'
                                      AND COALESCE(contact_number,'') <> '') AS apollo_phone,
                   COUNT(*) FILTER (WHERE COALESCE(official_email_id,'') = '') AS no_email
              FROM recruiters WHERE apollo_enriched_at >= :s AND apollo_enriched_at < :e
             GROUP BY harvest_source ORDER BY attempted DESC
        """, p):
            print(f"    {r['harvest_source'] or '-':<14} attempted={r['attempted']:<5} "
                  f"email={r['apollo_email']:<5} phone={r['apollo_phone']:<5} "
                  f"still_no_email={r['no_email']}")

        print("\n  Company-location HR reveals (company_location_candidates.revealed_at):")
        for r in await _q(db, """
            SELECT COUNT(*) AS revealed,
                   COUNT(*) FILTER (WHERE COALESCE(email,'') <> '') AS with_email,
                   COUNT(*) FILTER (WHERE no_email) AS no_email
              FROM company_location_candidates WHERE revealed_at >= :s AND revealed_at < :e
        """, p):
            print(f"    revealed={r['revealed']}  with_email={r['with_email']}  "
                  f"no_email={r['no_email']}")

        print("\n  Org lookups (companies — 1 credit each, never yield an email):")
        for r in await _q(db, """
            SELECT COUNT(*) FILTER (WHERE apollo_org_resolved_at >= :s AND apollo_org_resolved_at < :e)
                     AS org_resolves,
                   COUNT(*) FILTER (WHERE apollo_org_resolved_at >= :s AND apollo_org_resolved_at < :e
                                      AND COALESCE(apollo_org_id,'') = '') AS org_misses,
                   COUNT(*) FILTER (WHERE apollo_enriched_at >= :s AND apollo_enriched_at < :e)
                     AS org_enriched
              FROM companies
        """, p):
            print(f"    org resolve attempts={r['org_resolves']} (misses={r['org_misses']})  "
                  f"org enrich attempts={r['org_enriched']}")

        print("\n  Contact Finder reveals (recruiter_finder_reveal_log):")
        for r in await _q(db, """
            SELECT tenant_id, reveal_type, source, COUNT(*) AS reveals,
                   COUNT(*) FILTER (WHERE COALESCE(email,'') <> '') AS with_email,
                   COUNT(*) FILTER (WHERE COALESCE(phone,'') <> '') AS with_phone,
                   SUM(credits_spent) AS credits
              FROM recruiter_finder_reveal_log WHERE created_at >= :s AND created_at < :e
             GROUP BY tenant_id, reveal_type, source ORDER BY SUM(credits_spent) DESC
        """, p):
            print(f"    {r['tenant_id']:<10} {r['reveal_type']:<6} {r['source'] or '-':<15} "
                  f"reveals={r['reveals']:<4} email={r['with_email']:<4} "
                  f"phone={r['with_phone']:<4} credits={r['credits']}")

        print("\n  Phone reveals (apollo_phone_requests — 8 credits per delivered phone):")
        for r in await _q(db, """
            SELECT purpose, source, status, COUNT(*) AS n, SUM(credits_charged) AS credits
              FROM apollo_phone_requests WHERE requested_at >= :s AND requested_at < :e
             GROUP BY purpose, source, status ORDER BY SUM(credits_charged) DESC NULLS LAST
        """, p):
            print(f"    {r['purpose']:<11} {r['source']:<18} {r['status']:<10} "
                  f"n={r['n']:<5} credits={r['credits'] or 0}")

        print("\n  Harvest runs started this day (apollo_usage.run = Apollo's per-run figure):")
        for r in await _q(db, """
            SELECT id, COALESCE(source,'multi') AS source, status, started_at,
                   apollo_usage ->> 'run' AS run_credits
              FROM harvest_runs WHERE started_at >= :s AND started_at < :e ORDER BY started_at
        """, p):
            print(f"    {_ist_hour(r['started_at'])} IST  {r['source']:<9} {r['status']:<10} "
                  f"credits_this_run={r['run_credits']}  id={r['id']}")

        # ── Event log hint ───────────────────────────────────────────────────
        _hdr("NEXT: correlate the 'unseen spend' hours with the api logs")
        print("  docker compose logs api --since <hour> | grep -E "
              "'apollo_(credits_used|matched_no_contact|no_match|credit_settled|"
              "webhook_url_rejected|http_error)'")
        print("  Many apollo_matched_no_contact lines in a gap hour = Apollo billed matches "
              "that returned no email (the app books those as 0).")
        print("  A gap hour with NO apollo_* log lines = spend outside this app "
              "(Apollo UI exports / another integration using the same key).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="IST day, YYYY-MM-DD (default: today IST)")
    ap.add_argument("--no-live", action="store_true", help="skip the live Apollo reading")
    a = ap.parse_args()
    d = date.fromisoformat(a.date) if a.date else datetime.now(IST).date()
    asyncio.run(main(d, live=not a.no_live))
