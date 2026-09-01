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

import html as html_lib

import structlog

from app.models.harvest_models import NotificationConfig
from app.services.email_service import EmailSender
from app.services.report_service import build_excel_report_bytes, build_json_report_bytes

logger = structlog.get_logger(__name__)

_DEFAULT_SUBJECT = "Harvest Report — {run_id} ({status}, {total_jobs} jobs)"


def _render_insights(insights: dict | None) -> str:
    """A plain-text 'Harvest insights' block for a completed run — source split,
    contactability, and contact provenance (local-LLM/scraped vs Apollo). Returns
    "" when no insights were supplied so the email degrades to the basic summary."""
    if not insights:
        return ""
    g = insights.get
    return (
        "Harvest insights\n\n"
        "Sources\n"
        f"- With LinkedIn: {g('linkedin', 0)}\n"
        f"- Without LinkedIn: {g('non_linkedin', 0)} (Naukri {g('naukri', 0)}, Dice {g('dice', 0)})\n\n"
        "Contactable records\n"
        f"- With email: {g('with_email', 0)}\n"
        f"- With contact number: {g('with_phone', 0)}\n\n"
        "Contact provenance\n"
        f"- Extracted via local LLM: {g('local_llm_contacts', 0)}\n"
        f"- Enriched via Apollo: {g('apollo_enriched', 0)}\n\n"
    )


def _render_body(
    run_id: str,
    status: str,
    total_jobs: int,
    sources: list[str],
    error: str = "",
    insights: dict | None = None,
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
        f"{_render_insights(insights)}"
        "The extracted jobs are attached to this email.\n\n"
        "Regards,\n"
        "Sightspectrum Harvest Agent"
    )


def _esc(value: object) -> str:
    return html_lib.escape(str(value if value is not None else ""))


def _insights_html(insights: dict | None) -> str:
    """The 'Harvest insights' block as a simple HTML table so the numbers line up
    in a column (the point of using HTML over the ragged plain-text version).
    Returns "" when no insights so the email degrades to just the summary."""
    if not insights:
        return ""
    g = lambda k: insights.get(k, 0)  # noqa: E731

    def section(title: str) -> str:
        return f'<tr><td colspan="2" style="padding:14px 0 2px;font-weight:700;">{_esc(title)}</td></tr>'

    def row(label: str, value: object) -> str:
        return (
            f'<tr><td style="padding:3px 0;color:#333;">{label}</td>'
            f'<td style="padding:3px 0 3px 28px;text-align:right;white-space:nowrap;">{_esc(value)}</td></tr>'
        )

    rows = (
        section("Sources")
        + row("With LinkedIn", g("linkedin"))
        + row(f'Without LinkedIn (Naukri {_esc(g("naukri"))}, Dice {_esc(g("dice"))})', g("non_linkedin"))
        + section("Contactable records")
        + row("With email", g("with_email"))
        + row("With contact number", g("with_phone"))
        + section("Contact provenance")
        + row("Extracted via local LLM", g("local_llm_contacts"))
        + row("Enriched via Apollo", g("apollo_enriched"))
    )
    return (
        '<p style="margin:16px 0 2px;font-weight:700;">Harvest insights</p>'
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;font-size:14px;">{rows}</table>'
    )


def _render_body_html(
    run_id: str,
    status: str,
    total_jobs: int,
    sources: list[str],
    error: str = "",
    insights: dict | None = None,
) -> str:
    """Simple HTML version of the report email — plain, readable text with the
    insights in a small aligned table. `_render_body` is the plain-text fallback
    for clients that don't render HTML."""
    src = ", ".join(sources) or "-"
    is_failed = status == "failed"

    if is_failed:
        intro = f"The harvest run <b>{_esc(run_id)}</b> failed before it could produce a report."
        summary = (
            f"Status: {_esc(status)}<br>Sources: {_esc(src)}<br>Error: {_esc(error or 'unknown')}"
        )
        middle = "<p style=\"margin:0;\">No report is attached — the run did not reach the export step.</p>"
    else:
        intro = f"The harvest run <b>{_esc(run_id)}</b> has completed."
        summary = f"Status: {_esc(status)}<br>Sources: {_esc(src)}<br>Total jobs: {_esc(total_jobs)}"
        middle = _insights_html(insights) + '<p style="margin:16px 0 0;">The extracted jobs are attached to this email.</p>'

    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Arial,sans-serif;'
        'font-size:14px;line-height:1.6;color:#222;max-width:600px;">'
        '<p style="margin:0 0 14px;">Hello,</p>'
        f'<p style="margin:0 0 14px;">{intro}</p>'
        f'<p style="margin:0 0 14px;">{summary}</p>'
        f"{middle}"
        '<p style="margin:18px 0 0;color:#666;">Regards,<br>Sightspectrum Harvest Agent</p>'
        "</div>"
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
    insights: dict | None = None,
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

    body = _render_body(run_id, status, total_jobs, sources, error, insights)
    html_body = _render_body_html(run_id, status, total_jobs, sources, error, insights)

    try:
        log.info("harvest_report_email_sending", recipients=recipients, attachments=len(attachments))
        await email_sender.send_email_with_attachments(
            recipients, subject, body, attachment_blobs=attachments, html_body=html_body,
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
