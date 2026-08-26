"""
Apollo enrichment from a CSV export — fill in the email & contact_number columns.
=================================================================================

Reads a CSV produced by ``scripts/export_linkedin_contacts.sql`` and, for every
row that HAS a LinkedIn profile URL but is MISSING both email and phone, calls
Apollo (by LinkedIn URL) to reveal contact details and writes them back into the
CSV's ``email`` and ``contact_number`` columns.

This is CSV-in → CSV-out only. It does NOT write anything to the database — the
``recruiter_id`` column (if present) is carried through unchanged. If you later
want these contacts reflected in the app's UI/reports (which read from the DB),
you'd import them separately; this script intentionally leaves the DB untouched.

Safety / credits
────────────────
Apollo reveals cost credits, so this script:
  * only calls Apollo for rows missing BOTH email and phone,
  * supports --dry-run (no calls, no file changes) and --limit N (cap calls).

Phone note: Apollo delivers phones asynchronously via a webhook this app does not
expose, so phones will not resolve synchronously — only emails do. Phone reveal
is off unless settings.apollo_reveal_phone is true (or you pass --reveal-phone).

Usage
─────
    # dry run — show what WOULD be enriched, spend nothing, write nothing:
    python scripts/enrich_from_csv.py --csv data/window_21_25.csv --dry-run

    # real run → writes data/window_21_25_enriched.csv:
    python scripts/enrich_from_csv.py --csv data/window_21_25.csv --limit 100

    # overwrite the input file in place, and reveal phones too:
    python scripts/enrich_from_csv.py --csv data/today.csv --in-place --reveal-phone

Uses whatever APOLLO_* settings resolve to in the app config (app/config.py).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from pathlib import Path

# Allow `python scripts/enrich_from_csv.py` from the project root — scripts/ is
# added to sys.path[0], not the project root, so `app` wouldn't otherwise import.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.services.apollo_enrichment import apollo_contact_fallback

# CSV header aliases — the export uses the first name in each list, but accept
# common alternates so hand-edited or differently-aliased CSVs still work.
_COL_ALIASES: dict[str, list[str]] = {
    "linkedin":    ["linkedin_profile_url", "linkedin_url", "linkedin"],
    "name":        ["poc", "job_poster_name", "poc_name", "name"],
    "company":     ["company", "company_name"],
    "email":       ["email", "email_id"],
    "mobile":      ["mobile", "contact_number", "phone"],
}
# Header names used when the input CSV doesn't already have an email/phone column.
_EMAIL_OUT_DEFAULT = "email"
_MOBILE_OUT_DEFAULT = "contact_number"


def _build_header_map(fieldnames: list[str]) -> dict[str, str]:
    """Map each logical column to the actual CSV header (case-insensitive)."""
    lower_to_actual = {(h or "").strip().lower(): h for h in fieldnames}
    resolved: dict[str, str] = {}
    for logical, aliases in _COL_ALIASES.items():
        for alias in aliases:
            if alias in lower_to_actual:
                resolved[logical] = lower_to_actual[alias]
                break
    return resolved


def _get(row: dict[str, str], header_map: dict[str, str], logical: str) -> str:
    actual = header_map.get(logical)
    if not actual:
        return ""
    return (row.get(actual) or "").strip()


class _Counters:
    def __init__(self) -> None:
        self.total = 0
        self.no_linkedin = 0
        self.has_contact_in_csv = 0
        self.apollo_attempted = 0
        self.email_found = 0
        self.phone_found = 0
        self.matched = 0
        self.would_enrich = 0        # dry-run only
        self.errors = 0

    def summary(self) -> str:
        return (
            "\n------------ Summary ------------\n"
            f"  rows read ................... {self.total}\n"
            f"  skipped: no linkedin url .... {self.no_linkedin}\n"
            f"  skipped: contact in csv ..... {self.has_contact_in_csv}\n"
            f"  apollo calls attempted ...... {self.apollo_attempted}\n"
            f"    - matched ................. {self.matched}\n"
            f"    - email found ............. {self.email_found}\n"
            f"    - phone found ............. {self.phone_found}\n"
            f"  would-enrich (dry-run) ...... {self.would_enrich}\n"
            f"  errors ...................... {self.errors}\n"
        )


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.reveal_phone:
        settings.apollo_reveal_phone = True  # in-process override only

    if not settings.apollo_api_key and not args.dry_run:
        print("ERROR: APOLLO_API_KEY is not configured — nothing to enrich with. "
              "Set it in the .env, or use --dry-run to preview.", file=sys.stderr)
        return 2

    csv_path = Path(args.csv)
    if not csv_path.is_file():
        print(f"ERROR: CSV not found: {csv_path}", file=sys.stderr)
        return 2

    if not settings.apollo_reveal_phone and not args.dry_run:
        print("NOTE: phone reveal is OFF (settings.apollo_reveal_phone=false) — "
              "only emails will be revealed. Pass --reveal-phone to enable.")

    # ── Read the whole CSV into memory (preserve order + all columns) ────────
    # utf-8-sig strips a BOM if the CSV was exported with one so the first
    # header isn't mangled (matches the app's prior csv-encoding fix).
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            print("ERROR: CSV has no header row.", file=sys.stderr)
            return 2
        fieldnames = list(reader.fieldnames)
        rows = list(reader)

    header_map = _build_header_map(fieldnames)
    if "linkedin" not in header_map:
        print(f"ERROR: no LinkedIn URL column found. Looked for "
              f"{_COL_ALIASES['linkedin']}, got headers {fieldnames}", file=sys.stderr)
        return 2

    # Resolve (or create) the columns we write into.
    email_col = header_map.get("email") or _EMAIL_OUT_DEFAULT
    mobile_col = header_map.get("mobile") or _MOBILE_OUT_DEFAULT
    for col in (email_col, mobile_col):
        if col not in fieldnames:
            fieldnames.append(col)

    from app.services.apollo_client import ApolloClient
    client = ApolloClient(settings) if settings.apollo_api_key else None

    c = _Counters()

    for row in rows:
        c.total += 1

        li_url = _get(row, header_map, "linkedin")
        if not li_url:
            c.no_linkedin += 1
            continue

        # Only enrich rows missing BOTH email and phone.
        existing_email = (row.get(email_col) or "").strip()
        existing_phone = (row.get(mobile_col) or "").strip()
        if existing_email or existing_phone:
            c.has_contact_in_csv += 1
            continue

        name = _get(row, header_map, "name")
        company = _get(row, header_map, "company")

        if args.dry_run:
            c.would_enrich += 1
            print(f"  [dry-run] would enrich: {name or '(no name)'} <{li_url}>")
            continue

        if args.limit is not None and c.apollo_attempted >= args.limit:
            print(f"\nReached --limit {args.limit} Apollo calls; stopping.")
            break

        # ── The single Apollo call (no DB, no cooldown — CSV is the input set) ──
        try:
            apollo = await apollo_contact_fallback(
                settings=settings,
                linkedin_url=li_url,
                person_name=name,
                company_name=company,
                already_email=False,
                already_phone=False,
                apollo_enriched_at=None,   # no DB cooldown for a pure CSV pass
                client=client,
            )
        except Exception as exc:
            c.errors += 1
            print(f"  ! apollo call failed for {li_url}: {exc}", file=sys.stderr)
            continue

        if apollo.attempted:
            c.apollo_attempted += 1
        if apollo.matched:
            c.matched += 1
        if apollo.email:
            c.email_found += 1
            row[email_col] = apollo.email
        if apollo.phone:
            c.phone_found += 1
            row[mobile_col] = apollo.phone

        status = "email+phone" if (apollo.email and apollo.phone) else \
                 "email" if apollo.email else \
                 "phone" if apollo.phone else "no contact"
        print(f"  [ok] {name or '(no name)'} <{li_url}> -> {status}")

    # ── Write the enriched CSV out ───────────────────────────────────────────
    if not args.dry_run:
        out_path = csv_path if args.in_place else \
            (Path(args.out) if args.out else csv_path.with_name(f"{csv_path.stem}_enriched{csv_path.suffix}"))
        with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nWrote {len(rows)} rows -> {out_path}")

    print(c.summary())
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fill a CSV's email & contact_number columns via Apollo (no DB writes).")
    p.add_argument("--csv", required=True, help="Path to the CSV exported from export_linkedin_contacts.sql")
    p.add_argument("--out", default=None, help="Output CSV path (default: <input>_enriched.csv).")
    p.add_argument("--in-place", action="store_true", help="Overwrite the input CSV instead of writing a new file.")
    p.add_argument("--dry-run", action="store_true", help="Preview only - no Apollo calls, no file writes.")
    p.add_argument("--limit", type=int, default=None, help="Cap the number of Apollo calls (credit safety).")
    p.add_argument("--reveal-phone", action="store_true", help="Force phone reveal (overrides settings.apollo_reveal_phone).")
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(_parse_args())))
