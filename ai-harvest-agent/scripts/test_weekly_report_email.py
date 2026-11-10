"""
Test script: send the weekly harvest report email using recent run data.

Usage:
    cd ai-harvest-agent
    python scripts/test_weekly_report_email.py                      # current week's runs
    python scripts/test_weekly_report_email.py --days 14            # last 14 days of runs
    python scripts/test_weekly_report_email.py --to you@example.com  # override recipient
    python scripts/test_weekly_report_email.py --dry-run             # render HTML to file

Computes the same weekly stats the Friday scheduled job would, but lets you
control the window and recipient. --dry-run writes the HTML to a local file
for browser preview.

Requires a running PostgreSQL and a valid .env with SMTP credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.core.dependencies import get_session_factory  # noqa: E402
from app.services.email_service import EmailSender, report_identity_from  # noqa: E402
from app.services.harvest_notification_service import _IST, _to_ist  # noqa: E402
from app.services.weekly_report_service import (  # noqa: E402
    compute_weekly_stats,
    render_weekly_report_html,
)


async def main():
    parser = argparse.ArgumentParser(description="Test weekly harvest report email")
    parser.add_argument(
        "--days", type=int, default=7,
        help="Look-back window in days (default: 7, i.e. this week)",
    )
    parser.add_argument("--to", help="Override recipient email address")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Render HTML to a local file instead of sending",
    )
    args = parser.parse_args()

    settings = get_settings()
    factory = get_session_factory(settings)

    # ── Compute week window ─────────────────────────────────────────────
    now = datetime.now(_IST)
    if args.days == 7:
        # Default: Monday 00:00 → now
        days_since_monday = now.weekday()
        week_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=days_since_monday
        )
    else:
        week_start = now - timedelta(days=args.days)
    week_end = now

    week_start_utc = week_start.astimezone(timezone.utc)
    week_end_utc = week_end.astimezone(timezone.utc)

    print(f"Window: {week_start.strftime('%Y-%m-%d %H:%M IST')} → "
          f"{week_end.strftime('%Y-%m-%d %H:%M IST')}")

    # ── Compute stats ───────────────────────────────────────────────────
    async with factory() as db:
        stats = await compute_weekly_stats(
            db, week_start=week_start_utc, week_end=week_end_utc,
        )

    if not stats:
        print("No harvest runs found in the given window.")
        sys.exit(1)

    print(f"\n  Total runs:      {stats['total_runs']}")
    print(f"  Total jobs:      {stats['total_jobs']}")
    print(f"  Direct clients:  {stats['total_direct_clients']}")
    print(f"  Per-run entries:  {len(stats['per_run'])}")
    for pr in stats["per_run"]:
        print(f"    {pr['run_id']}  {pr['status']}  {pr['jobs']} jobs  "
              f"{pr['direct_clients']} DC  {', '.join(pr['sources'])}")
    print(f"  Source stats:    {stats['source_stats']}")

    html = render_weekly_report_html(stats)

    if args.dry_run:
        out_dir = Path(__file__).parent
        html_path = out_dir / "test_weekly_report_email.html"
        html_path.write_text(html, encoding="utf-8")
        print(f"\nHTML rendered to {html_path}")
        print("Open the HTML in a browser to preview the email template.")
        return

    # ── Send ────────────────────────────────────────────────────────────
    from app.services.config_service import ConfigService
    config = ConfigService().load()
    recipients = config.notifications.recipients if config.notifications else []
    if args.to:
        recipients = [args.to]
    elif not recipients:
        recipient = input("Recipient email (or press Enter for SMTP default): ").strip()
        recipients = [recipient] if recipient else [settings.smtp_sender_mail]

    ws = _to_ist(stats["week_start"])
    we = _to_ist(stats["week_end"])
    date_range = (
        f"{ws.strftime('%d %b')} – {we.strftime('%d %b %Y')}" if ws and we
        else "This Week"
    )
    subject = (
        f"\U0001f4ca [TEST] Weekly Harvest Report — {date_range} "
        f"({stats['total_jobs']} jobs, {stats['total_runs']} runs)"
    )
    sender_identity = report_identity_from(settings, "JobHarvest Weekly Report")
    sender = EmailSender(settings)

    print(f"\nSending to: {recipients}")
    await sender.send_email_with_attachments(
        recipients, subject,
        "Your weekly harvest report is available — view the HTML version of this email.",
        html_body=html,
        sender_identity=sender_identity,
    )
    print("Done — check your inbox.")


if __name__ == "__main__":
    asyncio.run(main())
