"""
Test script: send the harvest notification email using yesterday's (or any
recent) run data from the database.

Usage:
    cd ai-harvest-agent
    python scripts/test_harvest_email.py                      # latest run
    python scripts/test_harvest_email.py --run-id 20261029_103000  # specific run
    python scripts/test_harvest_email.py --to you@example.com  # override recipient
    python scripts/test_harvest_email.py --dry-run             # render HTML to file, don't send

Goes through the real send_harvest_report() path (same insights, phase timings,
Apollo figures, sender identity and attachments as a live run). --dry-run swaps
in a capturing sender, so the preview is exactly what would be sent.

Requires a running PostgreSQL and a valid .env with SMTP credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Ensure the project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core.dependencies import get_session_factory  # noqa: E402
from app.models.harvest_config import NotificationConfig  # noqa: E402
from app.models.harvest_run import HarvestRunORM, ScrapedJobORM  # noqa: E402
from app.services import apollo_budget, apollo_credit_usage  # noqa: E402
from app.services.config_service import ConfigService  # noqa: E402
from app.services.email_service import EmailSender  # noqa: E402
from app.services.harvest_notification_service import send_harvest_report  # noqa: E402
from app.services.report_service import (  # noqa: E402
    compute_harvest_insights,
    merged_job_dicts,
)


class _CapturingSender:
    """Stands in for EmailSender on --dry-run: records the message instead of sending."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.captured: dict = {}

    async def send_email_with_attachments(self, recipients, subject, body, **kw) -> None:
        self.captured = {"recipients": recipients, "subject": subject, "body": body, **kw}


async def _find_latest_run(factory) -> HarvestRunORM | None:
    async with factory() as db:
        result = await db.execute(
            select(HarvestRunORM)
            .where(HarvestRunORM.status.in_(["success", "completed", "stopped"]))
            .order_by(HarvestRunORM.completed_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def _find_run_by_display_id(factory, run_id: str) -> HarvestRunORM | None:
    async with factory() as db:
        result = await db.execute(
            select(HarvestRunORM).where(HarvestRunORM.run_id == run_id).limit(1)
        )
        return result.scalar_one_or_none()


async def _load_jobs(factory, run_pk: str) -> list[ScrapedJobORM]:
    async with factory() as db:
        result = await db.execute(
            select(ScrapedJobORM).where(ScrapedJobORM.run_id == run_pk)
        )
        return list(result.scalars())


async def main():
    parser = argparse.ArgumentParser(description="Test harvest notification email")
    parser.add_argument("--run-id", help="Display run_id (e.g. 20261029_103000)")
    parser.add_argument("--to", help="Override recipient email address")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render HTML to a local file instead of sending",
    )
    args = parser.parse_args()

    settings = get_settings()
    factory = get_session_factory(settings)

    # ── Find the run ────────────────────────────────────────────────────
    if args.run_id:
        run = await _find_run_by_display_id(factory, args.run_id)
        if not run:
            print(f"No run found with run_id = {args.run_id!r}")
            sys.exit(1)
    else:
        run = await _find_latest_run(factory)
        if not run:
            print("No completed runs found in the database.")
            sys.exit(1)

    print(f"Using run: {run.run_id}  (pk={run.id})")
    print(f"  Status:    {run.status}")
    print(f"  Sources:   {run.sources}")
    print(f"  Started:   {run.started_at}")
    print(f"  Completed: {run.completed_at}")
    print(f"  Combined:  {run.combined_count} jobs")

    # ── Load jobs & compute insights ────────────────────────────────────
    jobs = await _load_jobs(factory, run.id)
    print(f"  Loaded:    {len(jobs)} ScrapedJobORM rows")

    insights = compute_harvest_insights(jobs) if jobs else None
    job_dicts = merged_job_dicts(jobs)

    # ── Build run timing ────────────────────────────────────────────────
    run_timing = None
    if run.started_at and run.completed_at:
        elapsed = (run.completed_at - run.started_at).total_seconds()
        run_timing = {
            "started": run.started_at,
            "completed": run.completed_at,
            "duration_s": elapsed,
            "phases": run.phase_timings or {},
        }

    # ── Apollo figures (same as the real email path) ────────────────────
    # Cycle remaining comes from Apollo's own counter; "credits this run" is the
    # local ledger's enrichment spend within the run's own window (not up to now,
    # so re-running the script later doesn't pick up newer spend).
    apollo_usage = await apollo_credit_usage.report_usage(None, run.started_at)
    print(f"  Apollo usage: {apollo_usage}")
    if insights is not None and run.started_at:
        run_credits = await apollo_budget.credits_between(
            run.started_at, run.completed_at, purpose="enrichment"
        )
        print(f"  Apollo enrichment credits this run: {run_credits}")
        if run_credits is not None:
            insights["apollo_credits_run"] = run_credits

    # ── Notification config: the live one, retargeted for the test ──────
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

    # ── Dry run: write what would have been sent ────────────────────────
    if not sender.captured:
        print("\nNothing rendered — see data/logs/app.log for harvest_report_email_*.")
        sys.exit(1)
    out_dir = Path(__file__).parent
    html_path = out_dir / "test_harvest_email.html"
    html_path.write_text(sender.captured["html_body"], encoding="utf-8")
    print(f"\nSubject: {sender.captured['subject']}")
    print(f"From:    {sender.captured['sender_identity']}")
    print(f"HTML rendered to {html_path}")
    for name, blob in sender.captured["attachment_blobs"]:
        (out_dir / name).write_bytes(blob)
        print(f"Attachment written: {out_dir / name}")
    print("Open the HTML in a browser to preview the email template.")


if __name__ == "__main__":
    asyncio.run(main())
