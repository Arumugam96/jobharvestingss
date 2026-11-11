"""
Weekly harvest report email — aggregates all harvest runs from the past week
(Monday→Friday) and sends a summary report every Friday at 18:00 IST.

Uses the same Outlook-safe HTML helpers from harvest_notification_service.py.
Registered as an APScheduler cron job in app/main.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.harvest_run import HarvestRunORM, ScrapedJobORM
from app.models.recruiter import RecruiterORM
from app.services.email_service import EmailSender, report_identity_from
from app.services.harvest_notification_service import (
    _IST,
    _bg_cell,
    _cell,
    _divider,
    _doc,
    _esc,
    _fmt_duration,
    _kv_row,
    _row,
    _section_title,
    _src_badge,
    _src_badges,
    _table,
    _TABLE,
    _to_ist,
    _LINE,
    _MUTED,
)
from app.services.harvest_run_service import db_read

logger = structlog.get_logger(__name__)

_DONE_STATUSES = ("success", "completed", "no_results")
_SOURCE_ORDER = ("LinkedIn", "Naukri", "Dice")


async def _week_runs(
    db: AsyncSession, *, week_start: datetime, week_end: datetime,
) -> list[HarvestRunORM]:
    """All harvest run-history runs that started within the given week window."""
    H = HarvestRunORM
    return list((await db.execute(
        select(H).where(
            or_(H.source.is_(None), H.source == "LinkedIn Feed"),
            H.started_at >= week_start,
            H.started_at < week_end,
        ).order_by(H.started_at.asc())
    )).scalars().all())


async def _week_source_stats(
    db: AsyncSession, run_ids: list[str],
) -> dict[str, dict[str, int]]:
    """Per-source job counts + contact counts for the given runs."""
    if not run_ids:
        return {}
    J, R = ScrapedJobORM, RecruiterORM
    rows = (await db.execute(
        select(
            J.source,
            func.count(J.id).label("jobs"),
        )
        .where(J.run_id.in_(run_ids))
        .group_by(J.source)
    )).all()
    return {r.source: {"jobs": r.jobs} for r in rows if r.source}


async def compute_weekly_stats(
    db: AsyncSession, *, week_start: datetime, week_end: datetime,
) -> dict[str, Any] | None:
    """Compute the weekly report data: run summaries + aggregates."""
    runs = await _week_runs(db, week_start=week_start, week_end=week_end)
    if not runs:
        return None

    run_ids = [r.id for r in runs]
    source_stats = await _week_source_stats(db, run_ids)

    total_jobs = sum(r.combined_count or 0 for r in runs)
    total_duration = sum(
        ((r.completed_at - r.started_at).total_seconds() if r.completed_at and r.started_at else 0)
        for r in runs
    )

    per_run = []
    for r in runs:
        dur = (
            (r.completed_at - r.started_at).total_seconds()
            if r.completed_at and r.started_at else 0
        )
        per_run.append({
            "run_id": r.run_id,
            "sources": r.sources or [],
            "status": r.status,
            "jobs": r.combined_count or 0,
            "duration_s": dur,
            "is_feed": r.source == "LinkedIn Feed",
        })

    return {
        "total_runs": len(runs),
        "total_jobs": total_jobs,
        "total_duration_s": total_duration,
        "per_run": per_run,
        "source_stats": source_stats,
        "week_start": week_start,
        "week_end": week_end,
    }


def _status_color(status: str) -> str:
    return {
        "success": "#10b981", "completed": "#10b981", "failed": "#ef4444",
        "stopped": "#f59e0b", "running": "#3b82f6", "no_results": "#6b7280",
    }.get(status, _MUTED)


def render_weekly_report_html(stats: dict[str, Any]) -> str:
    """Render the weekly harvest report as Outlook-safe HTML."""
    ws = _to_ist(stats["week_start"])
    we = _to_ist(stats["week_end"])
    date_range = (
        f"{ws.strftime('%d %b')} – {we.strftime('%d %b %Y')}" if ws and we
        else "This Week"
    )

    # ── Header ──────────────────────────────────────────────────────────
    week_sources = [
        n for n in _SOURCE_ORDER
        if any(n in pr["sources"] for pr in stats["per_run"] if not pr["is_feed"])
    ]
    header = (
        f'<div style="font-size:11px;font-weight:700;letter-spacing:0.1em;'
        f'text-transform:uppercase;color:{_MUTED};margin-bottom:8px;">Sightspectrum</div>'
        f'<div style="font-size:24px;font-weight:800;margin:0;">Weekly Harvest Report</div>'
        f'<table {_TABLE} style="border-collapse:collapse;"><tr>'
        + _cell(_esc(date_range) + (" &middot;" if week_sources else ""),
                f"padding:4px 10px 0 0;font-size:13px;color:{_MUTED};white-space:nowrap;",
                'valign="middle"')
        + _cell(_src_badges(week_sources), "padding:4px 0 0;", 'valign="middle"')
        + "</tr></table>"
    )

    # ── Hero metric ─────────────────────────────────────────────────────
    hero = (
        f'<div style="font-size:64px;font-weight:900;line-height:1;color:#0ea5e9;">'
        f'{_esc(stats["total_jobs"])}</div>'
        f'<div style="font-size:13px;font-weight:600;letter-spacing:0.06em;'
        f'text-transform:uppercase;color:{_MUTED};margin-top:6px;">Jobs Harvested This Week</div>'
    )

    # ── Stat pills ──────────────────────────────────────────────────────
    pills = [
        (stats["total_runs"], "Runs"),
        (_fmt_duration(stats["total_duration_s"]), "Total Duration"),
    ]
    pill_cells = []
    for value, label in pills:
        pill_cells.append(_bg_cell(
            f'<b>{_esc(value)}</b> <span style="color:{_MUTED};">{_esc(label)}</span>',
            "#f9fafb",
            f"padding:8px 14px;font-size:13px;border:1px solid {_LINE};white-space:nowrap;",
            'align="center"',
        ))
    pills_html = (
        f'<table {_TABLE} align="center" style="border-collapse:collapse;margin:0 auto;"><tr>'
        + '<td width="8" style="width:8px;font-size:0;line-height:0;">&nbsp;</td>'.join(
            pill_cells)
        + "</tr></table>"
    )

    # ── Week summary ────────────────────────────────────────────────────
    summary_rows = (
        _kv_row("Total Jobs", str(stats["total_jobs"]))
        + _kv_row("Total Runs", str(stats["total_runs"]))
        + _kv_row("Total Duration", _fmt_duration(stats["total_duration_s"]))
    )
    summary = _section_title("Week Summary") + _table(summary_rows)

    # ── Per-run breakdown table ─────────────────────────────────────────
    def _th(text: str, align: str, width: str = "") -> str:
        w = f' width="{width}"' if width else ""
        return _cell(
            text,
            f"padding:0 0 6px;font-size:10px;font-weight:700;letter-spacing:0.08em;"
            f"text-transform:uppercase;color:{_MUTED};text-align:{align};",
            f'align="{align}"{w}',
        )

    def _td(content: str, align: str, last: bool = False) -> str:
        top = f"border-top:2px solid {_LINE};" if last else f"border-top:1px solid {_LINE};"
        weight = "800" if last else "600"
        return _cell(
            content, f"padding:6px 0;{top}font-size:13px;font-weight:{weight};"
            f"text-align:{align};", f'align="{align}" valign="middle"',
        )

    run_header = (
        "<tr>"
        + _th("Run ID", "left", "140")
        + _th("Sources", "left", "110")
        + _th("Status", "center", "80")
        + _th("Jobs", "center", "60")
        + _th("Duration", "center", "80")
        + "</tr>"
    )
    run_rows = ""
    for pr in stats["per_run"]:
        status_html = (
            f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
            f'font-size:11px;font-weight:700;color:#fff;'
            f'background:{_status_color(pr["status"])};">{_esc(pr["status"])}</span>'
        )
        sources_html = (
            _src_badge("LinkedIn Feed", 18, 12, label="Feed") if pr["is_feed"]
            else _src_badges(pr["sources"], 18, gap=6, show_names=False)
        )
        run_rows += (
            "<tr>"
            + _td(_esc(pr["run_id"]), "left")
            + _td(sources_html, "left")
            + _td(status_html, "center")
            + _td(str(pr["jobs"]), "center")
            + _td(_fmt_duration(pr["duration_s"]), "center")
            + "</tr>"
        )
    # Total row
    run_rows += (
        "<tr>"
        + _td("Total", "left", last=True)
        + _td("", "left", last=True)
        + _td("", "center", last=True)
        + _td(f'<b>{stats["total_jobs"]}</b>', "center", last=True)
        + _td(f'<b>{_fmt_duration(stats["total_duration_s"])}</b>', "center", last=True)
        + "</tr>"
    )
    breakdown = _section_title("Per-Run Breakdown") + _table(run_header + run_rows)

    # ── Source breakdown ────────────────────────────────────────────────
    src_header = (
        "<tr>"
        + _th("Source", "left", "200")
        + _th("Jobs", "center")
        + "</tr>"
    )
    src_rows = ""
    total_src_jobs = 0
    for name in _SOURCE_ORDER:
        s = stats["source_stats"].get(name, {})
        jobs = s.get("jobs", 0)
        total_src_jobs += jobs
        if not jobs:
            continue
        src_rows += (
            "<tr>" + _td(_src_badge(name, 20, 13), "left") + _td(str(jobs), "center") + "</tr>"
        )

    if src_rows:
        src_rows += (
            "<tr>"
            + _td("Total", "left", last=True)
            + _td(f'<b>{total_src_jobs}</b>', "center", last=True)
            + "</tr>"
        )
        source_section = _section_title("Source Breakdown") + _table(src_header + src_rows)
    else:
        source_section = ""

    # ── Footer ──────────────────────────────────────────────────────────
    footer = (
        "Weekly report auto-generated every Friday at 18:00 IST.<br>"
        "Sightspectrum Harvest Agent"
    )

    rows = (
        _row(header, "5px 20px 0")
        + _row(hero, "10px 10px 0px", "text-align:center;")
        + _row(pills_html, "0 20px 16px", "text-align:center;")
        + _divider()
        + _row(summary, "8px 20px 8px")
        + _divider()
        + _row(breakdown, "8px 20px 8px")
    )
    if source_section:
        rows += _divider() + _row(source_section, "8px 20px 8px")
    rows += (
        _divider(0)
        + _row(footer, "12px 20px", f"text-align:center;font-size:12px;color:{_MUTED};")
    )

    return _doc(f"Weekly Harvest Report — {date_range}", rows, "#0ea5e9",
                "linear-gradient(135deg,#0ea5e9,#8b5cf6)")


async def send_weekly_report() -> bool:
    """Compute and send the weekly harvest report. Called by APScheduler every Friday.
    Returns True when an email was sent. Never raises."""
    try:
        now = datetime.now(_IST)
        # Week window: Monday 00:00 IST → Friday 23:59 IST of the current week.
        # If today is Friday (weekday=4), go back to Monday.
        days_since_monday = now.weekday()  # 0=Mon, 4=Fri
        week_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=days_since_monday
        )
        week_end = now  # up to current moment

        # Convert to UTC for DB queries
        week_start_utc = week_start.astimezone(timezone.utc)
        week_end_utc = week_end.astimezone(timezone.utc)

        stats = await db_read(
            lambda db: compute_weekly_stats(
                db, week_start=week_start_utc, week_end=week_end_utc
            )
        )

        if not stats:
            logger.info("weekly_report_skip_no_runs")
            return False

        html = render_weekly_report_html(stats)
        settings = get_settings()

        from app.services.config_service import ConfigService
        config = ConfigService().load()
        recipients = (
            config.notifications.recipients if config.notifications else []
        )
        if not recipients:
            recipients = [settings.outreach_auto_reply_to or settings.smtp_sender_mail]

        ws = _to_ist(stats["week_start"])
        we = _to_ist(stats["week_end"])
        date_range = (
            f"{ws.strftime('%d %b')} – {we.strftime('%d %b %Y')}" if ws and we
            else "This Week"
        )

        sender = EmailSender(settings)
        sender_identity = report_identity_from(settings, "JobHarvest Weekly Report")
        subject = (
            f"\U0001f4ca Weekly Harvest Report — {date_range} "
            f"({stats['total_jobs']} jobs, {stats['total_runs']} runs)"
        )

        from app.services.harvest_notification_service import collect_source_icon_attachments
        icon_atts = collect_source_icon_attachments(html)
        await sender.send_email_with_attachments(
            recipients,
            subject,
            "Your weekly harvest report is available — view the HTML version of this email.",
            html_body=html,
            sender_identity=sender_identity,
            inline_attachments=icon_atts,
        )
        logger.info(
            "weekly_report_sent",
            recipients=len(recipients),
            total_runs=stats["total_runs"],
            total_jobs=stats["total_jobs"],
        )
        return True
    except Exception as exc:
        logger.warning("weekly_report_failed", error=str(exc))
        return False
