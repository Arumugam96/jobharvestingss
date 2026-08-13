"""
Sends the post-harvest report email (Excel/JSON attachments) once a harvest
run completes — called from both the manual (POST /run-harvest-agent) and
scheduled (APScheduler auto-run) completion paths.

The attachments are generated in memory from the run's job records (with
each job's RecruiterORM contact info already merged in — see
app/services/report_service.py); no result file is written to disk anymore.

Everything is driven by HarvestConfig.notifications (harvest_config.json):
enable/disable, recipient list, which formats to attach (excel/json/both),
and the subject line template — no code change needed to retarget or restyle.
"""
from __future__ import annotations

import structlog

from app.models.harvest_models import NotificationConfig
from app.services.email_service import EmailSender
from app.services.report_service import build_excel_report_bytes, build_json_report_bytes

logger = structlog.get_logger(__name__)

_DEFAULT_SUBJECT = "Harvest Report — {run_id} ({status}, {total_jobs} jobs)"


def _render_body(
    run_id: str, status: str, total_jobs: int, sources: list[str], error: str = ""
) -> str:
    if status == "failed":
        return (
            "Hello,\n\n"
            f"The harvest run '{run_id}' failed before it could produce a report.\n\n"
            f"Status:     {status}\n"
            f"Sources:    {', '.join(sources) or '-'}\n"
            f"Error:      {error or 'unknown'}\n\n"
            "No report is attached — the run did not reach the export step.\n\n"
            "Regards,\n"
            "Sightspectrum Harvest Agent"
        )
    return (
        "Hello,\n\n"
        f"The harvest run '{run_id}' has completed.\n\n"
        f"Status:     {status}\n"
        f"Sources:    {', '.join(sources) or '-'}\n"
        f"Total jobs: {total_jobs}\n\n"
        "The extracted jobs are attached to this email.\n\n"
        "Regards,\n"
        "Sightspectrum Harvest Agent"
    )


async def send_harvest_report(
    email_sender: EmailSender,
    notifications: NotificationConfig,
    *,
    run_id: str,
    status: str,
    total_jobs: int,
    sources: list[str],
    job_dicts: list[dict] | None = None,
    error: str = "",
) -> None:
    """Email the harvest report (or a failure alert) to notifications.recipients,
    if enabled. The JSON/Excel attachments are built in memory from job_dicts
    (job records with recruiter contact info merged in). Logs every step so a
    delivery problem is diagnosable from data/logs/app.log instead of silently
    disappearing.

    Never raises — a misconfigured/unreachable SMTP server must not fail the
    harvest run itself; errors are logged (with full traceback) and swallowed.
    """
    log = logger.bind(run_id=run_id, status=status)
    log.info("harvest_report_email_check_start", notifications_enabled=notifications.enabled)

    if not notifications.enabled:
        log.info("harvest_report_email_skipped", reason="notifications_disabled")
        return

    recipients = [r.strip() for r in notifications.recipients if r.strip()]
    if not recipients:
        log.warning("harvest_report_email_skipped", reason="no_recipients")
        return
    log.debug("harvest_report_recipients_resolved", recipients=recipients)

    attachments: list[tuple[str, bytes]] = []
    if job_dicts:
        if notifications.report_format in ("excel", "both"):
            excel_bytes = build_excel_report_bytes(job_dicts)
            if excel_bytes:
                attachments.append((f"{run_id}_harvest.xlsx", excel_bytes))
        if notifications.report_format in ("json", "both"):
            attachments.append(
                (f"{run_id}_harvest.json", build_json_report_bytes(job_dicts, run_id=run_id))
            )
    log.debug(
        "harvest_report_attachments_resolved",
        report_format=notifications.report_format,
        job_records=len(job_dicts or []),
        resolved=[name for name, _ in attachments],
    )

    # A failed run never reached the export step, so there's nothing to
    # attach — still send the alert so the failure isn't silent. A
    # completed run with no attachments is a real problem worth skipping on
    # (nothing useful to send).
    if not attachments and status != "failed":
        log.warning(
            "harvest_report_email_skipped",
            reason="no_attachments_available",
            report_format=notifications.report_format,
        )
        return

    try:
        subject = notifications.subject_template.format(
            run_id=run_id, status=status, total_jobs=total_jobs
        )
    except (KeyError, IndexError):
        log.warning(
            "harvest_report_subject_template_invalid", template=notifications.subject_template
        )
        subject = _DEFAULT_SUBJECT.format(run_id=run_id, status=status, total_jobs=total_jobs)
    log.debug("harvest_report_subject_built", subject=subject)

    body = _render_body(run_id, status, total_jobs, sources, error)

    try:
        log.info("harvest_report_email_sending", recipients=recipients, attachments=len(attachments))
        await email_sender.send_email_with_attachments(
            recipients, subject, body, attachment_blobs=attachments,
        )
        log.info(
            "harvest_report_email_sent",
            recipients=recipients,
            attachments=[name for name, _ in attachments],
        )
    except Exception:
        log.exception(
            "harvest_report_email_failed",
            recipients=recipients,
            attachments=len(attachments),
        )
