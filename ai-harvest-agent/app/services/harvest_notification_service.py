"""
Sends the post-harvest report email (Excel/JSON attachments) once a harvest
run completes — called from both the manual (POST /run-harvest-agent) and
scheduled (APScheduler auto-run) completion paths.

Everything is driven by HarvestConfig.notifications (harvest_config.json):
enable/disable, recipient list, which files to attach (excel/json/both), and
the subject line template — no code change needed to retarget or restyle.
"""
from __future__ import annotations

from pathlib import Path

import structlog

from app.models.harvest_models import NotificationConfig
from app.services.email_service import EmailSender

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
    excel_path: str = "",
    json_path: str = "",
    error: str = "",
) -> None:
    """Email the harvest report (or a failure alert) to notifications.recipients,
    if enabled. Logs every step so a delivery problem is diagnosable from
    data/logs/app.log instead of silently disappearing.

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

    attachments: list[str] = []
    if notifications.report_format in ("excel", "both") and excel_path and Path(excel_path).is_file():
        attachments.append(excel_path)
    if notifications.report_format in ("json", "both") and json_path and Path(json_path).is_file():
        attachments.append(json_path)
    log.debug(
        "harvest_report_attachments_resolved",
        report_format=notifications.report_format,
        excel_path=excel_path,
        json_path=json_path,
        resolved=attachments,
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
        await email_sender.send_email_with_attachments(recipients, subject, body, attachments)
        log.info(
            "harvest_report_email_sent",
            recipients=recipients,
            attachments=[Path(p).name for p in attachments],
        )
    except Exception:
        log.exception(
            "harvest_report_email_failed",
            recipients=recipients,
            attachments=len(attachments),
        )
