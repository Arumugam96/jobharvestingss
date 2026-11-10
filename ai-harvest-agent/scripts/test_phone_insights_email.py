"""
Test script: send the phone outreach insights email using a recent run.

Usage:
    cd ai-harvest-agent
    python scripts/test_phone_insights_email.py                      # latest eligible run
    python scripts/test_phone_insights_email.py --run-id 20261029_103000  # specific run
    python scripts/test_phone_insights_email.py --to you@example.com  # override recipient
    python scripts/test_phone_insights_email.py --dry-run             # render HTML to file

Goes through the real compute + render path but bypasses the claim mechanism
so the same run can be tested repeatedly. --dry-run writes the HTML to a local
file for preview without sending.

Requires a running PostgreSQL and a valid .env with SMTP credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core.dependencies import get_session_factory  # noqa: E402
from app.models.harvest_run import HarvestRunORM  # noqa: E402
from app.services.email_service import EmailSender, report_identity_from  # noqa: E402
from app.services.phone_insights_service import (  # noqa: E402
    compute_phone_outreach_insights,
    render_phone_insights_email_html,
)


async def _find_latest_run(factory) -> HarvestRunORM | None:
    async with factory() as db:
        result = await db.execute(
            select(HarvestRunORM)
            .where(
                HarvestRunORM.status.in_(["success", "completed", "no_results"]),
                HarvestRunORM.source.is_(None),  # multi-source runs only
            )
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


async def main():
    parser = argparse.ArgumentParser(description="Test phone outreach insights email")
    parser.add_argument("--run-id", help="Display run_id (e.g. 20261029_103000)")
    parser.add_argument("--to", help="Override recipient email address")
    parser.add_argument(
        "--dry-run", action="store_true",
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
            print("No completed multi-source runs found in the database.")
            sys.exit(1)

    print(f"Using run: {run.run_id}  (pk={run.id})")
    print(f"  Status:    {run.status}")
    print(f"  Sources:   {run.sources}")
    print(f"  Tenant:    {run.tenant_id}")
    print(f"  Completed: {run.completed_at}")
    print(f"  Phone insights already sent: {run.phone_insights_sent_at}")

    # ── Compute insights (bypass claim) ─────────────────────────────────
    async with factory() as db:
        insights = await compute_phone_outreach_insights(db, run.id, run.tenant_id)

    print(f"\n  Total phones:  {insights['total_phones']}")
    print(f"  Called:         {insights['called']}")
    print(f"  Not called:     {insights['not_called']}")
    print(f"  Answered:       {insights['answered']}")
    print(f"  Unanswered:     {insights['unanswered']}")
    for s in insights["sources"]:
        print(f"  {s['label']}: {s['count']} phones, {s['called']} called ({s['coverage']}%)")

    if insights["total_phones"] == 0:
        print("\n⚠ No phone contacts for this run — the real flow would skip the email.")
        if not args.dry_run:
            print("  Use --dry-run to still see what the (empty) email would look like.")

    sources_label = " + ".join(run.sources or []) or "Harvest"
    run_label = run.run_id
    html = render_phone_insights_email_html(insights, run_label, sources=run.sources)

    if args.dry_run:
        out_dir = Path(__file__).parent
        html_path = out_dir / "test_phone_insights_email.html"
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

    subject = (
        f"\U0001f4de [TEST] Phone Outreach Insights — "
        f"24h after {sources_label} Harvest ({run.run_id})"
    )
    sender_identity = report_identity_from(settings, "JobHarvest Phone Insights")
    sender = EmailSender(settings)

    print(f"\nSending to: {recipients}")
    await sender.send_email_with_attachments(
        recipients, subject,
        "Phone outreach insights are available — view the HTML version of this email.",
        html_body=html,
        sender_identity=sender_identity,
    )
    print("Done — check your inbox.")


if __name__ == "__main__":
    asyncio.run(main())
