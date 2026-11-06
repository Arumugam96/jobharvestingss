"""
Phone outreach insights email — sent 24 hours after a harvest run completes.
Derives call activity from the phone_call_logs table for jobs in the run.
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.harvest_run import ScrapedJobORM
from app.models.phone_call_log import PhoneCallLogORM
from app.services.email_service import EmailSender, report_identity_from
from app.services.harvest_run_service import db_read
from app.config import get_settings

logger = structlog.get_logger(__name__)


async def compute_phone_outreach_insights(
    db: AsyncSession, run_id: str, tenant_id: str,
) -> dict[str, Any]:
    """Compute phone outreach insights for a completed harvest run.

    ``run_id`` is the harvest_runs surrogate PK (UUID), which is what
    ScrapedJobORM.run_id references as an FK.
    """
    # 1. Get all job IDs from this run
    job_ids_q = select(ScrapedJobORM.id).where(
        ScrapedJobORM.run_id == run_id,
        ScrapedJobORM.tenant_id == tenant_id,
    )
    job_ids_result = await db.execute(job_ids_q)
    job_ids = [r[0] for r in job_ids_result.all()]

    if not job_ids:
        return {"total_phones": 0, "called": 0, "not_called": 0, "answered": 0,
                "unanswered": 0, "sources": [], "run_id": run_id}

    # 2. Count jobs that have at least one phone number (contact_number).
    phones_q = select(func.count()).select_from(ScrapedJobORM).where(
        ScrapedJobORM.run_id == run_id,
        ScrapedJobORM.tenant_id == tenant_id,
        ScrapedJobORM.contact_number.isnot(None),
        ScrapedJobORM.contact_number != "",
    )
    phones_result = await db.execute(phones_q)
    total_phones = phones_result.scalar() or 0

    # 3. Get call logs for these jobs (non-cleared only)
    calls_q = select(PhoneCallLogORM).where(
        PhoneCallLogORM.job_id.in_(job_ids),
        PhoneCallLogORM.tenant_id == tenant_id,
        PhoneCallLogORM.cleared_at.is_(None),
    )
    calls_result = await db.execute(calls_q)
    calls = calls_result.scalars().all()

    # Distinct jobs that were called
    called_job_ids = {c.job_id for c in calls}
    called = len(called_job_ids)
    not_called = max(0, total_phones - called)

    answered = sum(1 for c in calls if c.call_status == "answered")
    unanswered = sum(1 for c in calls if c.call_status == "unanswered")

    # 4. Phone source coverage — group call logs by phone_source
    source_q = (
        select(
            PhoneCallLogORM.phone_source,
            func.count(func.distinct(PhoneCallLogORM.phone)).label("count"),
        )
        .where(
            PhoneCallLogORM.job_id.in_(job_ids),
            PhoneCallLogORM.tenant_id == tenant_id,
            PhoneCallLogORM.cleared_at.is_(None),
        )
        .group_by(PhoneCallLogORM.phone_source)
    )
    source_result = await db.execute(source_q)
    source_data = {r.phone_source: r.count for r in source_result.all()}

    # Build source coverage list (Job Posting and Recruiter only)
    source_map = {"job": "Job Posting", "recruiter": "Recruiter (Apollo)"}
    sources = []
    for key, label in source_map.items():
        called_count = source_data.get(key, 0)
        sources.append({
            "label": label,
            "count": total_phones,
            "called": called_count,
            "coverage": round(called_count / total_phones * 100, 1) if total_phones > 0 else 0,
        })

    return {
        "total_phones": total_phones,
        "called": called,
        "not_called": not_called,
        "answered": answered,
        "unanswered": unanswered,
        "called_pct": round(called / total_phones * 100, 1) if total_phones > 0 else 0,
        "not_called_pct": round(not_called / total_phones * 100, 1) if total_phones > 0 else 0,
        "sources": sources,
        "run_id": run_id,
    }


def render_phone_insights_email_html(insights: dict[str, Any], run_label: str) -> str:
    """Render the phone outreach insights as an HTML email with inline CSS."""
    total = insights["total_phones"]
    called = insights["called"]
    not_called = insights["not_called"]
    answered = insights["answered"]
    unanswered = insights["unanswered"]
    called_pct = insights["called_pct"]
    not_called_pct = insights["not_called_pct"]
    sources = insights["sources"]

    # Build source rows HTML
    source_rows = ""
    for s in sources:
        cov_color = "#16A34A" if s["coverage"] >= 30 else "#D97706"
        source_rows += f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #E2E8F0;text-align:left;font-weight:500">{s["label"]}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #E2E8F0;text-align:center">{s["count"]}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #E2E8F0;text-align:center">{s["called"]}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #E2E8F0;text-align:center">
            <span style="font-weight:600;color:{cov_color}">{s["coverage"]}%</span>
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#F1F5F9;font-family:Arial,Helvetica,sans-serif;color:#1E293B">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;margin:0 auto">
  <!-- Header -->
  <tr>
    <td style="background:linear-gradient(135deg,#1E40AF,#2563EB);padding:28px 32px 24px;text-align:center">
      <h1 style="margin:0;font-size:20px;font-weight:700;color:#ffffff;letter-spacing:-.01em">Phone Outreach Insights</h1>
      <div style="display:inline-block;margin-top:10px;background:rgba(255,255,255,.15);color:#fff;font-size:11px;font-weight:600;padding:3px 12px;border-radius:12px;letter-spacing:.02em">{run_label}</div>
    </td>
  </tr>
  <!-- Overview -->
  <tr>
    <td style="padding:24px 32px">
      <div style="font-size:14px;font-weight:700;color:#1E293B;margin:0 0 14px;padding-bottom:8px;border-bottom:2px solid #2563EB;display:inline-block">Overview</div>
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td width="33%" style="padding:0 5px 10px 0">
            <div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:10px;padding:14px 16px;text-align:center">
              <div style="font-size:28px;font-weight:700;color:#2563EB;line-height:1.1">{total}</div>
              <div style="font-size:11px;color:#64748B;margin-top:4px;line-height:1.3">Total phone contacts<br>from harvest</div>
            </div>
          </td>
          <td width="33%" style="padding:0 5px 10px">
            <div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:10px;padding:14px 16px;text-align:center">
              <div style="font-size:28px;font-weight:700;color:#16A34A;line-height:1.1">{called}</div>
              <div style="font-size:11px;color:#64748B;margin-top:4px;line-height:1.3">Phone Outreach<br><span style="font-weight:600;color:#16A34A">{called_pct}%</span></div>
            </div>
          </td>
          <td width="33%" style="padding:0 0 10px 5px">
            <div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:10px;padding:14px 16px;text-align:center">
              <div style="font-size:28px;font-weight:700;color:#D97706;line-height:1.1">{not_called}</div>
              <div style="font-size:11px;color:#64748B;margin-top:4px;line-height:1.3">Not yet Outreached<br><span style="font-weight:600;color:#D97706">{not_called_pct}%</span></div>
            </div>
          </td>
        </tr>
        <tr>
          <td colspan="3" style="padding:0 0 0">
            <table cellpadding="0" cellspacing="0" style="margin:0 auto">
              <tr>
                <td style="padding:0 5px">
                  <div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:10px;padding:14px 24px;text-align:center;min-width:140px">
                    <div style="font-size:28px;font-weight:700;color:#16A34A;line-height:1.1">{answered}</div>
                    <div style="font-size:11px;color:#64748B;margin-top:4px;line-height:1.3">Contacted</div>
                  </div>
                </td>
                <td style="padding:0 5px">
                  <div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:10px;padding:14px 24px;text-align:center;min-width:140px">
                    <div style="font-size:28px;font-weight:700;color:#DC2626;line-height:1.1">{unanswered}</div>
                    <div style="font-size:11px;color:#64748B;margin-top:4px;line-height:1.3">Outreached but Unanswered</div>
                  </div>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </td>
  </tr>
  <!-- Phone Source Coverage -->
  <tr>
    <td style="padding:0 32px 24px">
      <div style="font-size:14px;font-weight:700;color:#1E293B;margin:0 0 14px;padding-bottom:8px;border-bottom:2px solid #2563EB;display:inline-block">Phone Source Coverage</div>
      <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:13px">
        <thead>
          <tr>
            <th style="text-align:left;font-size:11px;font-weight:600;letter-spacing:.03em;text-transform:uppercase;color:#64748B;padding:8px 12px;border-bottom:1px solid #E2E8F0">Phone Source</th>
            <th style="text-align:center;font-size:11px;font-weight:600;letter-spacing:.03em;text-transform:uppercase;color:#64748B;padding:8px 12px;border-bottom:1px solid #E2E8F0">Count</th>
            <th style="text-align:center;font-size:11px;font-weight:600;letter-spacing:.03em;text-transform:uppercase;color:#64748B;padding:8px 12px;border-bottom:1px solid #E2E8F0">Called</th>
            <th style="text-align:center;font-size:11px;font-weight:600;letter-spacing:.03em;text-transform:uppercase;color:#64748B;padding:8px 12px;border-bottom:1px solid #E2E8F0">Coverage</th>
          </tr>
        </thead>
        <tbody>
          {source_rows}
        </tbody>
      </table>
    </td>
  </tr>
  <!-- Footer -->
  <tr>
    <td style="padding:20px 32px;text-align:center;font-size:11px;color:#64748B;border-top:1px solid #E2E8F0;line-height:1.5">
      This report was auto-generated 24 hours after Harvest Run completed.<br>
      Insights are based on phone call logs recorded in Phone Tracker.<br>
      <span style="color:#2563EB">Sight Spectrum &middot; JobHarvest Platform</span>
    </td>
  </tr>
</table>
</body>
</html>"""


async def send_phone_outreach_insights(
    run_id: str, tenant_id: str, run_display_id: str, sources_label: str,
) -> None:
    """Compute + send the phone outreach insights email for a completed harvest run.

    ``run_id`` is the harvest_runs surrogate PK (UUID).
    ``run_display_id`` is the human-readable timestamp id (e.g. 20261105_143000).
    """
    try:
        settings = get_settings()

        result = await db_read(
            lambda db: compute_phone_outreach_insights(db, run_id, tenant_id)
        )
        if result is None or result["total_phones"] == 0:
            logger.info("phone_insights_skip_no_phones", run_id=run_id)
            return

        run_label = f"{sources_label} · {run_display_id}"
        html = render_phone_insights_email_html(result, run_label)

        sender = EmailSender(settings)
        subject = (
            f"\U0001f4de Phone Outreach Insights — "
            f"24h after {sources_label} Harvest ({run_display_id})"
        )

        # Send to the same recipients as the harvest report
        from app.services.config_service import ConfigService
        config = ConfigService().load()
        recipients = (
            config.notifications.email_recipients if config.notifications else []
        )
        if not recipients:
            recipients = [settings.outreach_auto_reply_to or settings.smtp_sender_mail]

        sender_identity = report_identity_from(settings, "JobHarvest Phone Insights")

        await sender.send_email_with_attachments(
            recipients,
            subject,
            "Phone outreach insights are available — view the HTML version of this email.",
            html_body=html,
            sender_identity=sender_identity,
        )
        logger.info("phone_insights_sent", run_id=run_id, recipients=len(recipients))
    except Exception as exc:
        logger.warning("phone_insights_failed", run_id=run_id, error=str(exc))
