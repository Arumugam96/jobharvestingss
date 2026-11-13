"""
Test script: send the weekly harvest report email using recent run data.

Usage:
    cd ai-harvest-agent
    python scripts/test_weekly_report_email.py                      # current week's runs
    python scripts/test_weekly_report_email.py --days 14            # last 14 days of runs
    python scripts/test_weekly_report_email.py --to you@example.com  # override recipient
    python scripts/test_weekly_report_email.py --dry-run             # render HTML to file
    python scripts/test_weekly_report_email.py --sample --dry-run    # sample data, no DB

Computes the same weekly stats the Friday scheduled job would, but lets you
control the window and recipient. --dry-run writes the HTML to a local file
for browser preview.

--sample bypasses the database entirely and feeds synthetic data to the
renderer so the email template can be previewed from a plain PowerShell
session with no running PostgreSQL.

Requires a running PostgreSQL and a valid .env with SMTP credentials
(unless --sample --dry-run).
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.harvest_notification_service import (  # noqa: E402
    _IST,
    _to_ist,
    collect_source_icon_attachments,
    embed_source_icons_for_preview,
)
from app.services.weekly_report_service import (  # noqa: E402
    render_weekly_report_html,
)


def _write_preview(path: Path, html: str) -> None:
    """Write the HTML with the inline logo attachments embedded, so the logos
    render when the file is opened in a browser."""
    atts = collect_source_icon_attachments(html)
    preview = embed_source_icons_for_preview(html, atts)
    path.write_text(preview, encoding="utf-8")
    missing = sorted(set(re.findall(r"cid:src-([\w-]+)", preview)))
    print(f"Source logos embedded: {[a['Filename'] for a in atts] or 'none'}")
    if missing:
        print(f"WARNING: no icon file for {missing} — these logos will be broken")


def _sample_stats() -> dict:
    now = datetime.now(_IST)
    days_since_monday = now.weekday()
    week_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=days_since_monday
    )
    week_end = now

    return {
        "total_runs": 5,
        "total_jobs": 312,
        "total_duration_s": 14520,
        "per_run": [
            {
                "run_id": "20261109_090000",
                "sources": ["LinkedIn", "Naukri"],
                "status": "success",
                "jobs": 124,
                "duration_s": 3120,
                "is_feed": False,
            },
            {
                "run_id": "20261109_143000",
                "sources": ["LinkedIn"],
                "status": "completed",
                "jobs": 68,
                "duration_s": 2400,
                "is_feed": False,
            },
            {
                "run_id": "20261108_100000",
                "sources": ["Naukri"],
                "status": "success",
                "jobs": 55,
                "duration_s": 4800,
                "is_feed": False,
            },
            {
                "run_id": "20261107_160000",
                "sources": ["LinkedIn"],
                "status": "success",
                "jobs": 42,
                "duration_s": 1800,
                "is_feed": True,
            },
            {
                "run_id": "20261106_110000",
                "sources": ["LinkedIn", "Naukri", "Dice"],
                "status": "stopped",
                "jobs": 23,
                "duration_s": 2400,
                "is_feed": False,
            },
        ],
        "source_stats": {
            "LinkedIn": {"jobs": 198},
            "Naukri": {"jobs": 96},
            "Dice": {"jobs": 18},
        },
        "week_start": week_start.astimezone(timezone.utc),
        "week_end": week_end.astimezone(timezone.utc),
    }


async def _sample_main(args):
    stats = _sample_stats()
    print(f"Using SAMPLE data (no DB)")
    print(f"  Total runs:      {stats['total_runs']}")
    print(f"  Total jobs:      {stats['total_jobs']}")
    print(f"  Per-run entries:  {len(stats['per_run'])}")
    for pr in stats["per_run"]:
        print(f"    {pr['run_id']}  {pr['status']}  {pr['jobs']} jobs  "
              f"{', '.join(pr['sources'])}")

    html = render_weekly_report_html(stats)

    if args.dry_run:
        out_dir = Path(__file__).parent
        html_path = out_dir / "test_weekly_report_email.html"
        _write_preview(html_path, html)
        print(f"\nHTML rendered to {html_path}")
        print("Open the HTML in a browser to preview the email template.")
        return

    # Send for real — needs --to and SMTP credentials from .env
    if not args.to:
        print("\n--sample without --dry-run requires --to <email>")
        print("Use: python scripts/test_weekly_report_email.py --sample --to you@example.com")
        sys.exit(1)

    from app.config import get_settings
    from app.services.email_service import EmailSender, report_identity_from

    settings = get_settings()
    ws = _to_ist(stats["week_start"])
    we = _to_ist(stats["week_end"])
    date_range = (
        f"{ws.strftime('%d %b')} – {we.strftime('%d %b %Y')}" if ws and we
        else "This Week"
    )
    subject = (
        f"\U0001f4ca [SAMPLE] Weekly Harvest Report — {date_range} "
        f"({stats['total_jobs']} jobs, {stats['total_runs']} runs)"
    )
    sender_identity = report_identity_from(settings, "JobHarvest Weekly Report")
    sender = EmailSender(settings)

    from app.services.harvest_notification_service import collect_source_icon_attachments
    icon_atts = collect_source_icon_attachments(html)

    print(f"\nSending to: {[args.to]}")
    await sender.send_email_with_attachments(
        [args.to], subject,
        "Your weekly harvest report is available — view the HTML version of this email.",
        html_body=html,
        sender_identity=sender_identity,
        inline_attachments=icon_atts,
    )
    print("Done — check your inbox.")


async def _db_main(args):
    from app.config import get_settings
    from app.core.dependencies import get_session_factory
    from app.services.email_service import EmailSender, report_identity_from
    from app.services.weekly_report_service import compute_weekly_stats

    settings = get_settings()
    factory = get_session_factory(settings)

    now = datetime.now(_IST)
    if args.days == 7:
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

    async with factory() as db:
        stats = await compute_weekly_stats(
            db, week_start=week_start_utc, week_end=week_end_utc,
        )

    if not stats:
        print("No harvest runs found in the given window.")
        sys.exit(1)

    print(f"\n  Total runs:      {stats['total_runs']}")
    print(f"  Total jobs:      {stats['total_jobs']}")
    print(f"  Per-run entries:  {len(stats['per_run'])}")
    for pr in stats["per_run"]:
        print(f"    {pr['run_id']}  {pr['status']}  {pr['jobs']} jobs  "
              f"{', '.join(pr['sources'])}")
    print(f"  Source stats:    {stats['source_stats']}")

    html = render_weekly_report_html(stats)

    if args.dry_run:
        out_dir = Path(__file__).parent
        html_path = out_dir / "test_weekly_report_email.html"
        _write_preview(html_path, html)
        print(f"\nHTML rendered to {html_path}")
        print("Open the HTML in a browser to preview the email template.")
        return

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

    from app.services.harvest_notification_service import collect_source_icon_attachments
    icon_atts = collect_source_icon_attachments(html)

    print(f"\nSending to: {recipients}")
    await sender.send_email_with_attachments(
        recipients, subject,
        "Your weekly harvest report is available — view the HTML version of this email.",
        html_body=html,
        sender_identity=sender_identity,
        inline_attachments=icon_atts,
    )
    print("Done — check your inbox.")


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
    parser.add_argument(
        "--sample", action="store_true",
        help="Use synthetic sample data — no database required",
    )
    args = parser.parse_args()

    if args.sample:
        await _sample_main(args)
    else:
        await _db_main(args)


if __name__ == "__main__":
    asyncio.run(main())
