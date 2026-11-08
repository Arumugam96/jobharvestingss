"""Test the harvest phone-reveal workflow end-to-end for a given recruiter.

Exercises the same code path the real harvest uses:

  1. **request** — calls outreach_phone_service.request_phone_after_outreach(),
     which checks eligibility (LinkedIn URL, company size, no existing phone,
     budget), creates a ``pending`` apollo_phone_requests row, and fires the
     Apollo reveal call.  With ``--skip-eligibility`` the five eligibility checks
     (LinkedIn URL, company-size band, existing phone, open request, budget) are
     bypassed so you can force a request for any recruiter.

  2. **simulate** — POSTs a fake Apollo delivery to the phone webhook (like
     simulate_apollo_phone_webhook.py) so the phone lands on the recruiter and
     shows up in the UI without waiting for Apollo.

  3. **status** — prints the recruiter's current phone state + any
     apollo_phone_requests rows.

Usage (from the project root or inside the container)
─────────────────────────────────────────────────────
    # See what would happen (dry run — default):
    python scripts/test_phone_reveal.py --recruiter-id <id>

    # Actually fire the Apollo request:
    python scripts/test_phone_reveal.py --recruiter-id <id> --live

    # Skip eligibility checks and force the request:
    python scripts/test_phone_reveal.py --recruiter-id <id> --live --skip-eligibility

    # After the request is pending, simulate Apollo delivering a phone:
    python scripts/test_phone_reveal.py --recruiter-id <id> --simulate --phone +12025550116

    # Just check the current state:
    python scripts/test_phone_reveal.py --recruiter-id <id> --status

    # List recent recruiters with Apollo person IDs (pick one to test):
    python scripts/test_phone_reveal.py --list-recruiters

Container:
    docker compose exec api python scripts/test_phone_reveal.py --recruiter-id <id>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.core.logging_config import configure_logging


# ── Helpers ─────────────────────────────────────────────────────────────────

async def _get_recruiter(recruiter_id: str) -> dict | None:
    from app.services.harvest_run_service import db_read
    from app.models.recruiter import RecruiterORM

    async def _q(db):
        rec = await db.get(RecruiterORM, recruiter_id)
        if rec is None:
            return None
        return {
            "id": rec.id, "tenant_id": rec.tenant_id, "name": rec.person_name,
            "company": rec.company_name, "designation": rec.designation,
            "linkedin_url": rec.linkedin_profile_url or "",
            "apollo_person_id": rec.apollo_person_id or "",
            "email": rec.official_email_id or "", "email_status": rec.email_status or "",
            "phone": rec.contact_number or "", "phone_status": rec.phone_status or "",
            "company_size": rec.company_size or "",
            "company_domain": rec.company_domain or "",
        }
    return await db_read(_q)


async def _get_phone_requests(recruiter_id: str) -> list[dict]:
    from sqlalchemy import select
    from app.models.apollo_phone import ApolloPhoneRequestORM
    from app.services.harvest_run_service import db_read

    async def _q(db):
        R = ApolloPhoneRequestORM
        rows = (await db.execute(
            select(R).where(R.recruiter_id == recruiter_id)
            .order_by(R.requested_at.desc()).limit(10)
        )).scalars().all()
        return [{
            "id": r.id, "ref": r.ref, "status": r.status,
            "apollo_person_id": r.apollo_person_id or "",
            "phone": r.phone or "", "phone_status": r.phone_status or "",
            "phone_type": r.phone_type or "", "credits_charged": r.credits_charged,
            "purpose": r.purpose, "source": r.source,
            "requested_at": r.requested_at.isoformat() if r.requested_at else "",
            "delivered_at": r.delivered_at.isoformat() if r.delivered_at else "",
        } for r in rows]
    return await db_read(_q) or []


async def _list_recruiters(limit: int = 20):
    from sqlalchemy import select
    from app.models.recruiter import RecruiterORM
    from app.services.harvest_run_service import db_read

    async def _q(db):
        R = RecruiterORM
        rows = (await db.execute(
            select(R).where(R.apollo_person_id.isnot(None), R.apollo_person_id != "")
            .order_by(R.last_seen_at.desc()).limit(limit)
        )).scalars().all()
        return [{
            "id": r.id, "name": r.person_name, "company": r.company_name,
            "apollo_person_id": r.apollo_person_id or "",
            "linkedin_url": (r.linkedin_profile_url or "")[:60],
            "phone": r.contact_number or "", "phone_status": r.phone_status or "",
            "tenant_id": r.tenant_id,
        } for r in rows]
    return await db_read(_q) or []


def _print_recruiter(rec: dict):
    print(f"\n{'─' * 60}")
    print(f"  Recruiter:  {rec['name']}  ({rec['designation']})")
    print(f"  Company:    {rec['company']}  (size: {rec['company_size'] or '?'})")
    print(f"  ID:         {rec['id']}")
    print(f"  Tenant:     {rec['tenant_id']}")
    print(f"  LinkedIn:   {rec['linkedin_url'] or '(none)'}")
    print(f"  Apollo PID: {rec['apollo_person_id'] or '(none)'}")
    print(f"  Email:      {rec['email']}  ({rec['email_status']})")
    print(f"  Phone:      {rec['phone'] or '(none)'}  ({rec['phone_status']})")
    print(f"{'─' * 60}")


def _print_requests(requests: list[dict]):
    if not requests:
        print("\n  No apollo_phone_requests rows for this recruiter.")
        return
    print(f"\n  Phone requests ({len(requests)}):")
    for r in requests:
        phone_display = r['phone'][:4] + '****' if len(r['phone']) > 4 else r['phone'] or '-'
        print(f"    [{r['status']:10}] ref={r['ref'][:8]}…  "
              f"person_id={r['apollo_person_id'][:12] or '-':12}  "
              f"phone={phone_display}  credits={r['credits_charged']}  "
              f"purpose={r['purpose']}  source={r['source']}  "
              f"requested={r['requested_at'][:19]}")


# ── Commands ────────────────────────────────────────────────────────────────

async def cmd_status(recruiter_id: str):
    rec = await _get_recruiter(recruiter_id)
    if rec is None:
        print(f"Recruiter {recruiter_id} not found.")
        return 1
    _print_recruiter(rec)
    requests = await _get_phone_requests(recruiter_id)
    _print_requests(requests)
    return 0


async def cmd_request(recruiter_id: str, *, live: bool, skip_eligibility: bool,
                      company_sizes: list[str] | None):
    rec = await _get_recruiter(recruiter_id)
    if rec is None:
        print(f"Recruiter {recruiter_id} not found.")
        return 1
    _print_recruiter(rec)

    settings = get_settings()
    print(f"\n  Settings check:")
    print(f"    APOLLO_API_KEY:       {'set' if settings.apollo_api_key else 'NOT SET'}")
    print(f"    APOLLO_REVEAL_PHONE:  {settings.apollo_reveal_phone}")
    print(f"    APOLLO_WEBHOOK_URL:   {settings.apollo_webhook_url or 'NOT SET'}")
    print(f"    APOLLO_WEBHOOK_TOKEN: {'set' if settings.apollo_webhook_token else 'NOT SET'}")

    if not live:
        print(f"\n  DRY RUN — would call request_phone_after_outreach(recruiter_id={recruiter_id!r})")
        print(f"  Pass --live to actually fire the Apollo request.")
        requests = await _get_phone_requests(recruiter_id)
        _print_requests(requests)
        return 0

    if skip_eligibility:
        print(f"\n  Bypassing eligibility — creating phone request directly…")
        from app.services import apollo_phone_service
        from app.services.apollo_client import ApolloClient, ApolloAPIError

        ref = await apollo_phone_service.begin_request(
            tenant_id=rec["tenant_id"], recruiter_id=recruiter_id,
            apollo_person_id=rec["apollo_person_id"] or None,
            purpose="enrichment", source="test_script",
            linkedin_url=rec["linkedin_url"] or None,
        )
        if not ref:
            print("  FAILED to create apollo_phone_requests row.")
            return 1
        print(f"  Created pending row  ref={ref}")

        client = ApolloClient(settings, purpose="enrichment")
        try:
            if rec["apollo_person_id"]:
                print(f"  Calling Apollo match_person_by_id({rec['apollo_person_id'][:20]}…, reveal_phone=True)")
                res = await client.match_person_by_id(
                    rec["apollo_person_id"], reveal_email=False, reveal_phone=True, webhook_ref=ref,
                )
            elif rec["linkedin_url"]:
                print(f"  Calling Apollo enrich_person_by_linkedin({rec['linkedin_url'][:40]}…, reveal_phone=True)")
                res = await client.enrich_person_by_linkedin(
                    rec["linkedin_url"], reveal_email=False, reveal_phone=True,
                    name=rec["name"], company=rec["company"], webhook_ref=ref,
                )
            else:
                print("  No Apollo person ID or LinkedIn URL — cannot call Apollo.")
                return 1
        except ApolloAPIError as exc:
            print(f"  Apollo API error: {exc}")
            from app.services.harvest_run_service import db_write
            await db_write(lambda db: apollo_phone_service.settle_request(db, ref, None))
            return 1

        from app.services.harvest_run_service import db_write
        ps = await db_write(lambda db: apollo_phone_service.settle_request(db, ref, res)) or ""
        print(f"  Settled: phone_status={ps}  matched={getattr(res, 'matched', False)}")
        if ps == apollo_phone_service.PS_PENDING:
            print(f"  Phone request is PENDING — Apollo will deliver via webhook.")
            print(f"  To simulate delivery now, run:")
            print(f"    python scripts/test_phone_reveal.py --recruiter-id {recruiter_id} --simulate")
    else:
        print(f"\n  Calling request_phone_after_outreach(recruiter_id={recruiter_id!r})…")
        from app.services.outreach_phone_service import request_phone_after_outreach

        result = await request_phone_after_outreach(
            recruiter_id=recruiter_id,
            company_sizes=company_sizes,
        )
        print(f"  Result: {result}")
        if result == "requested":
            print(f"  Phone request is PENDING — Apollo will deliver via webhook.")
            print(f"  To simulate delivery now, run:")
            print(f"    python scripts/test_phone_reveal.py --recruiter-id {recruiter_id} --simulate")
        elif result != "error":
            print(f"  Skipped: {result}")

    print()
    requests = await _get_phone_requests(recruiter_id)
    _print_requests(requests)
    return 0


async def cmd_simulate(recruiter_id: str, phone: str, base_url: str):
    rec = await _get_recruiter(recruiter_id)
    if rec is None:
        print(f"Recruiter {recruiter_id} not found.")
        return 1
    _print_recruiter(rec)

    requests = await _get_phone_requests(recruiter_id)
    pending = [r for r in requests if r["status"] == "pending"]
    if not pending:
        print("\n  No pending phone requests for this recruiter.")
        print("  Run --live first to create a request, or use --skip-eligibility.")
        return 1

    row = pending[0]
    ref = row["ref"]
    person_id = row["apollo_person_id"] or rec["apollo_person_id"]
    if not person_id:
        print("  No Apollo person ID on the request or recruiter — cannot simulate delivery.")
        return 1

    print(f"\n  Simulating webhook delivery:")
    print(f"    ref:       {ref}")
    print(f"    person_id: {person_id}")
    print(f"    phone:     {phone}")

    numbers = []
    if phone:
        numbers.append({
            "_id": "sim-0", "confidence_cd": "high",
            "direct_dial_source_cd": "contact_trusted",
            "dnc_other_info": {"country": "United States"},
            "dnc_status_cd": "not_found", "position": 0,
            "raw_number": phone, "sanitized_number": phone,
            "status_cd": "valid_number", "type_cd": "mobile",
        })
    payload = {
        "status": "success", "total_requested_enrichments": 1,
        "unique_enriched_records": 1 if phone else 0,
        "missing_records": 0 if phone else 1,
        "credits_consumed": 8 if phone else 0,
        "people": [{"id": person_id, "status": "success", "phone_numbers": numbers}],
    }

    settings = get_settings()
    token = settings.apollo_webhook_token or ""
    query: dict[str, str] = {"ref": ref}
    if token:
        query["token"] = token
    url = f"{base_url.rstrip('/')}/recruiter-finder/apollo-phone-webhook?{urllib.parse.urlencode(query)}"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                headers={"Content-Type": "application/json"})
    token_display = "***" if token else "(none)"
    print(f"\n  POST {base_url.rstrip('/')}/recruiter-finder/apollo-phone-webhook"
          f"?ref={ref[:8]}…&token={token_display}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_body = resp.read().decode(errors="replace")
            print(f"  {resp.status} {resp_body}")
    except urllib.error.HTTPError as exc:
        print(f"  {exc.code} {exc.read().decode(errors='replace')}")
        return 1
    except urllib.error.URLError as exc:
        print(f"  Connection failed: {exc.reason}")
        return 1

    print("\n  Verifying recruiter state after delivery…")
    rec2 = await _get_recruiter(recruiter_id)
    if rec2:
        print(f"    Phone:        {rec2['phone'] or '(none)'}")
        print(f"    Phone status: {rec2['phone_status']}")
    requests2 = await _get_phone_requests(recruiter_id)
    _print_requests(requests2)
    return 0


async def cmd_list_recruiters(limit: int):
    rows = await _list_recruiters(limit)
    if not rows:
        print("No recruiters with Apollo person IDs found.")
        return 0
    print(f"\nRecent recruiters with Apollo person IDs ({len(rows)}):\n")
    print(f"  {'ID':36}  {'Name':25}  {'Company':20}  {'Phone':15}  {'Status':10}  Tenant")
    print(f"  {'─' * 36}  {'─' * 25}  {'─' * 20}  {'─' * 15}  {'─' * 10}  {'─' * 12}")
    for r in rows:
        phone = r['phone'][:12] + '…' if len(r['phone']) > 12 else r['phone'] or '-'
        print(f"  {r['id']:36}  {r['name'][:25]:25}  {r['company'][:20]:20}  "
              f"{phone:15}  {r['phone_status'] or '-':10}  {r['tenant_id']}")
    return 0


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recruiter-id", default="",
                    help="Recruiter ID from the recruiters table")
    ap.add_argument("--live", action="store_true",
                    help="Actually fire the Apollo phone request (default is dry run)")
    ap.add_argument("--skip-eligibility", action="store_true",
                    help="Bypass outreach eligibility checks (LinkedIn, size, budget) "
                         "and call Apollo directly")
    ap.add_argument("--company-sizes", nargs="*", default=None,
                    help="Override company-size filter pills (e.g. '51-200' '201-500')")
    ap.add_argument("--simulate", action="store_true",
                    help="Simulate an Apollo webhook delivery for the newest pending request")
    ap.add_argument("--phone", default="+12025550116",
                    help="Phone number for --simulate (empty string = no number found)")
    ap.add_argument("--status", action="store_true",
                    help="Just print the recruiter's phone state and request rows")
    ap.add_argument("--list-recruiters", action="store_true",
                    help="List recent recruiters with Apollo person IDs")
    ap.add_argument("--base-url", default="http://localhost:8000",
                    help="App base URL for webhook simulation (default: http://localhost:8000)")
    args = ap.parse_args()

    configure_logging()

    if args.list_recruiters:
        return asyncio.run(cmd_list_recruiters(20))

    if not args.recruiter_id:
        ap.error("--recruiter-id is required (or use --list-recruiters to find one)")

    if args.status:
        return asyncio.run(cmd_status(args.recruiter_id))
    if args.simulate:
        return asyncio.run(cmd_simulate(args.recruiter_id, args.phone, args.base_url))
    return asyncio.run(cmd_request(
        args.recruiter_id, live=args.live, skip_eligibility=args.skip_eligibility,
        company_sizes=args.company_sizes,
    ))


if __name__ == "__main__":
    sys.exit(main())
