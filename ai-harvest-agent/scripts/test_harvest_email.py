"""
Test script: send the harvest notification email using yesterday's (or any
recent) run data from the database.

Usage:
    cd ai-harvest-agent
    python scripts/test_harvest_email.py                      # latest run
    python scripts/test_harvest_email.py --run-id 20261029_103000  # specific run
    python scripts/test_harvest_email.py --to you@example.com  # override recipient
    python scripts/test_harvest_email.py --dry-run             # render HTML to file, don't send
    python scripts/test_harvest_email.py --sample --dry-run    # sample data, no DB

Goes through the real send_harvest_report() path (same insights, phase timings,
Apollo figures, sender identity and attachments as a live run). --dry-run swaps
in a capturing sender, so the preview is exactly what would be sent.

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

from app.models.harvest_config import NotificationConfig  # noqa: E402
from app.services.harvest_notification_service import (  # noqa: E402
    embed_source_icons_for_preview,
    send_harvest_report,
)


def _write_preview(path: Path, captured: dict) -> None:
    """Write the captured HTML with the inline logo attachments embedded, so the
    logos render when the file is opened in a browser."""
    atts = captured.get("inline_attachments") or []
    html = embed_source_icons_for_preview(captured["html_body"], atts)
    path.write_text(html, encoding="utf-8")
    missing = sorted(set(re.findall(r"cid:src-([\w-]+)", html)))
    print(f"Source logos embedded: {[a['Filename'] for a in atts] or 'none'}")
    if missing:
        print(f"WARNING: no icon file for {missing} — these logos will be broken")


class _MockSettings:
    """Minimal settings stand-in for --sample mode (no .env required)."""
    otp_from_email = "sample-noreply@example.com"
    otp_from_name = "JobHarvest Sample"
    smtp_sender_mail = "sample@example.com"
    smtp_sender_name = "Sample"


class _CapturingSender:
    """Stands in for EmailSender on --dry-run: records the message instead of sending."""

    def __init__(self, settings=None) -> None:
        self.settings = settings or _MockSettings()
        self.captured: dict = {}

    async def send_email_with_attachments(self, recipients, subject, body, **kw) -> None:
        self.captured = {"recipients": recipients, "subject": subject, "body": body, **kw}


def _sample_insights() -> dict:
    return {
        "total": 124,
        "linkedin": 68,
        "non_linkedin": 56,
        "naukri": 42,
        "dice": 14,
        "with_email": 87,
        "with_phone": 47,
        "with_linkedin": 61,
        "with_contact": 95,
        "local_llm_contacts": 72,
        "apollo_enriched": 23,
        "apollo_credits_run": 23,
        "per_source": {
            "LinkedIn": {"jobs": 68, "email": 52, "phone": 28, "linkedin": 55, "poster_name": 60},
            "Naukri":   {"jobs": 42, "email": 28, "phone": 15, "linkedin": 4, "poster_name": 35},
            "Dice":     {"jobs": 14, "email": 7, "phone": 4, "linkedin": 2, "poster_name": 10},
        },
    }


def _sample_run_timing() -> dict:
    completed = datetime.now(timezone.utc)
    started = completed - timedelta(minutes=52)
    return {
        "started": started,
        "completed": completed,
        "duration_s": (completed - started).total_seconds(),
        "phases": {"scrape_s": 2400, "outreach_s": 720},
    }


def _sample_apollo_usage() -> dict:
    return {
        "consumed": 1820,
        "limit": 10000,
        "left_over": 8180,
        "today": 23,
        "run": 23,
        "cycle_start": "2026-11-01",
        "cycle_end": "2026-11-30",
        "taken_at": datetime.now(timezone.utc).isoformat(),
    }


_SAMPLE_SOURCES = ["LinkedIn", "Naukri"]
_SAMPLE_RUN_ID = "SAMPLE_20261110_120000"


async def _sample_main(args):
    """Render from synthetic data — no database required."""
    insights = _sample_insights()
    run_timing = _sample_run_timing()
    apollo_usage = _sample_apollo_usage()

    notif = NotificationConfig(
        enabled=True,
        recipients=["sample@example.com"],
        report_format="excel",
        email_cover_label="JobHarvest",
        subject_template="[SAMPLE] Harvest {status}: {total_jobs} jobs ({run_id})",
    )

    sender = _CapturingSender()
    await send_harvest_report(
        sender,
        notif,
        run_id=_SAMPLE_RUN_ID,
        status="success",
        total_jobs=insights["total"],
        sources=_SAMPLE_SOURCES,
        job_dicts=[],
        insights=insights,
        run_timing=run_timing,
        apollo_usage=apollo_usage,
    )

    if not sender.captured:
        print("Nothing rendered — check logs.")
        sys.exit(1)

    if args.dry_run:
        out_dir = Path(__file__).parent
        html_path = out_dir / "test_harvest_email.html"
        _write_preview(html_path, sender.captured)
        print(f"Subject: {sender.captured['subject']}")
        print(f"HTML rendered to {html_path}")
        print("Open the HTML in a browser to preview the email template.")
        return

    # Send for real — needs --to and SMTP credentials from .env
    if not args.to:
        print("\n--sample without --dry-run requires --to <email>")
        print("Use: python scripts/test_harvest_email.py --sample --to you@example.com")
        sys.exit(1)

    from app.config import get_settings
    from app.services.email_service import EmailSender

    settings = get_settings()
    real_sender = EmailSender(settings)

    notif = NotificationConfig(
        enabled=True,
        recipients=[args.to],
        report_format=None,
        email_cover_label="JobHarvest",
        subject_template="[SAMPLE] Harvest {status}: {total_jobs} jobs ({run_id})",
    )

    print(f"\nSending to: {[args.to]}")
    await send_harvest_report(
        real_sender,
        notif,
        run_id=_SAMPLE_RUN_ID,
        status="success",
        total_jobs=insights["total"],
        sources=_SAMPLE_SOURCES,
        job_dicts=[],
        insights=insights,
        run_timing=run_timing,
        apollo_usage=apollo_usage,
    )
    print("Done — check your inbox.")


async def _db_main(args):
    """Original DB-backed path."""
    from sqlalchemy import select

    from app.config import get_settings
    from app.core.dependencies import get_session_factory
    from app.models.harvest_run import HarvestRunORM, ScrapedJobORM
    from app.services import apollo_budget, apollo_credit_usage
    from app.services.config_service import ConfigService
    from app.services.email_service import EmailSender
    from app.services.report_service import compute_harvest_insights, merged_job_dicts

    settings = get_settings()
    factory = get_session_factory(settings)

    if args.run_id:
        async with factory() as db:
            result = await db.execute(
                select(HarvestRunORM).where(HarvestRunORM.run_id == args.run_id).limit(1)
            )
            run = result.scalar_one_or_none()
        if not run:
            print(f"No run found with run_id = {args.run_id!r}")
            sys.exit(1)
    else:
        async with factory() as db:
            result = await db.execute(
                select(HarvestRunORM)
                .where(HarvestRunORM.status.in_(["success", "completed", "stopped"]))
                .order_by(HarvestRunORM.completed_at.desc())
                .limit(1)
            )
            run = result.scalar_one_or_none()
        if not run:
            print("No completed runs found in the database.")
            sys.exit(1)

    print(f"Using run: {run.run_id}  (pk={run.id})")
    print(f"  Status:    {run.status}")
    print(f"  Sources:   {run.sources}")
    print(f"  Started:   {run.started_at}")
    print(f"  Completed: {run.completed_at}")
    print(f"  Combined:  {run.combined_count} jobs")

    async with factory() as db:
        result = await db.execute(
            select(ScrapedJobORM).where(ScrapedJobORM.run_id == run.id)
        )
        jobs = list(result.scalars())
    print(f"  Loaded:    {len(jobs)} ScrapedJobORM rows")

    insights = compute_harvest_insights(jobs) if jobs else None
    job_dicts = merged_job_dicts(jobs)

    run_timing = None
    if run.started_at and run.completed_at:
        elapsed = (run.completed_at - run.started_at).total_seconds()
        run_timing = {
            "started": run.started_at,
            "completed": run.completed_at,
            "duration_s": elapsed,
            "phases": run.phase_timings or {},
        }

    apollo_usage = await apollo_credit_usage.report_usage(None, run.started_at)
    print(f"  Apollo usage: {apollo_usage}")
    if insights is not None and run.started_at:
        run_credits = await apollo_budget.credits_between(
            run.started_at, run.completed_at, purpose="enrichment"
        )
        print(f"  Apollo enrichment credits this run: {run_credits}")
        if run_credits is not None:
            insights["apollo_credits_run"] = run_credits

    live = ConfigService().load().notifications
    recipient = args.to
    if not recipient and not args.dry_run:
        recipient = input("Recipient email (or press Enter for config default): ").strip()
    notif = NotificationConfig(
        enabled=True,
        recipients=[recipient] if recipient else (live.recipients or ["dry-run@example.com"]),
        report_format=live.report_format,
        email_cover_label=live.email_cover_label,
        subject_template="[TEST] " + live.subject_template,
    )

    sender = _CapturingSender(settings) if args.dry_run else EmailSender(settings)
    if not args.dry_run:
        print(f"\nSending to: {notif.recipients}")

    await send_harvest_report(
        sender,
        notif,
        run_id=run.run_id,
        status=run.status,
        total_jobs=insights["total"] if insights else len(jobs),
        sources=run.sources or [],
        job_dicts=job_dicts,
        insights=insights,
        run_timing=run_timing,
        apollo_usage=apollo_usage,
    )

    if not args.dry_run:
        print("Done — check your inbox (and data/logs/app.log for harvest_report_email_*).")
        return

    if not sender.captured:
        print("\nNothing rendered — see data/logs/app.log for harvest_report_email_*.")
        sys.exit(1)
    out_dir = Path(__file__).parent
    html_path = out_dir / "test_harvest_email.html"
    _write_preview(html_path, sender.captured)
    print(f"\nSubject: {sender.captured['subject']}")
    print(f"From:    {sender.captured['sender_identity']}")
    print(f"HTML rendered to {html_path}")
    for name, blob in sender.captured["attachment_blobs"]:
        (out_dir / name).write_bytes(blob)
        print(f"Attachment written: {out_dir / name}")
    print("Open the HTML in a browser to preview the email template.")


async def main():
    parser = argparse.ArgumentParser(description="Test harvest notification email")
    parser.add_argument("--run-id", help="Display run_id (e.g. 20261029_103000)")
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
