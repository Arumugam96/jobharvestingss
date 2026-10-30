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
from datetime import datetime, timezone

import structlog

from app.models.harvest_config import NotificationConfig
from app.services.apollo_budget import usage_this_month, usage_today
from app.services.email_service import EmailSender
from app.services.report_service import build_excel_report_bytes, build_json_report_bytes

logger = structlog.get_logger(__name__)

_DEFAULT_SUBJECT = "Harvest Report — {run_id} ({status}, {total_jobs} jobs)"


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s" if m else f"{s}s"


def _esc(value: object) -> str:
    return html_lib.escape(str(value if value is not None else ""))


def _pct(part: int, total: int) -> str:
    return f"{round(part * 100 / total)}%" if total else "0%"


# ── Plain-text fallback ─────────────────────────────────────────────────────

def _render_body(
    run_id: str,
    status: str,
    total_jobs: int,
    sources: list[str],
    error: str = "",
    insights: dict | None = None,
    run_timing: dict | None = None,
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

    lines = (
        "Hello,\n\n"
        f"The harvest run '{run_id}' has completed.\n\n"
        f"Status:     {status}\n"
        f"Sources:    {', '.join(sources) or '-'}\n"
        f"Total jobs: {total_jobs}\n\n"
    )
    if run_timing:
        started = run_timing.get("started")
        completed = run_timing.get("completed")
        dur = run_timing.get("duration_s", 0)
        lines += (
            "Run timing\n"
            f"- Start: {started.strftime('%d %b %Y, %H:%M:%S') if isinstance(started, datetime) else started}\n"
            f"- End:   {completed.strftime('%d %b %Y, %H:%M:%S') if isinstance(completed, datetime) else completed}\n"
            f"- Duration: {_fmt_duration(dur)}\n\n"
        )
    if insights:
        g = insights.get
        ps = g("per_source", {})
        lines += "Source breakdown\n"
        for src in ("LinkedIn", "Naukri", "Dice"):
            s = ps.get(src, {})
            if s.get("jobs"):
                lines += f"- {src}: {s['jobs']} jobs, {s.get('email',0)} email, {s.get('phone',0)} phone, {s.get('linkedin',0)} LinkedIn\n"
        lines += (
            f"\nContactable: {g('with_contact', 0)} of {g('total', 0)}\n"
            f"Apollo credits used: {g('apollo_enriched', 0)}\n\n"
        )
        nk = ps.get("Naukri", {})
        if nk.get("jobs"):
            lines += (
                "Naukri detail\n"
                f"- Email scraped: {nk.get('email', 0)}\n"
                f"- Phone scraped: {nk.get('phone', 0)}\n"
                f"- LinkedIn profile: {nk.get('linkedin', 0)}\n"
                f"- Poster name: {nk.get('poster_name', 0)}\n\n"
            )
    lines += (
        "The extracted jobs are attached to this email.\n\n"
        "Regards,\n"
        "Sightspectrum Harvest Agent"
    )
    return lines


# ── HTML email template ──────────────────────────────────────────────────────
# Single combined template: gradient header → hero metric → stat pills →
# run details → source breakdown table → Naukri detail → Apollo credits →
# contact coverage gauges → footer.

def _render_body_html(
    run_id: str,
    status: str,
    total_jobs: int,
    sources: list[str],
    error: str = "",
    insights: dict | None = None,
    run_timing: dict | None = None,
    apollo_daily: dict | None = None,
    apollo_monthly: dict | None = None,
) -> str:
    src_str = _esc(", ".join(sources) or "-")

    if status == "failed":
        return _render_failed_html(run_id, status, src_str, error)

    # ── Timing ───────────────────────────────────────────────────────────
    started = completed = duration = "-"
    if run_timing:
        s = run_timing.get("started")
        c = run_timing.get("completed")
        dur_s = run_timing.get("duration_s", 0)
        if isinstance(s, datetime):
            started = s.strftime("%H:%M:%S IST")
        if isinstance(c, datetime):
            completed = c.strftime("%H:%M:%S IST")
        duration = _fmt_duration(dur_s)

    run_date = "-"
    if run_timing:
        s = run_timing.get("started")
        if isinstance(s, datetime):
            run_date = s.strftime("%d %b %Y")

    # ── Insights ─────────────────────────────────────────────────────────
    g = (insights or {}).get
    ps = g("per_source", {})
    with_email = g("with_email", 0)
    with_phone = g("with_phone", 0)
    with_linkedin = g("with_linkedin", 0)
    with_contact = g("with_contact", 0)
    apollo = g("apollo_enriched", 0)
    total = total_jobs or 1  # avoid div/0

    status_color = {"completed": "#10b981", "failed": "#ef4444", "stopped": "#f59e0b"}.get(
        status, "#6b7280"
    )

    # ── Source rows ──────────────────────────────────────────────────────
    source_rows = ""
    src_configs = [
        ("LinkedIn", "li", "#0a66c2"),
        ("Naukri",   "nk", "#6366f1"),
        ("Dice",     "dc", "#ef4444"),
    ]
    for name, code, color in src_configs:
        s = ps.get(name, {})
        if not s.get("jobs"):
            continue
        source_rows += (
            f'<tr>'
            f'<td style="padding:14px 0;border-top:1px solid #e5e7eb;font-size:14px;">'
            f'<span style="display:inline-flex;align-items:center;gap:10px;font-weight:700;">'
            f'<span style="display:inline-block;width:28px;height:28px;border-radius:8px;'
            f'background:{color};color:#fff;font-size:12px;font-weight:800;line-height:28px;'
            f'text-align:center;">{_esc(code[:2].title())}</span>{_esc(name)}</span></td>'
            f'<td style="padding:14px 0;border-top:1px solid #e5e7eb;text-align:center;'
            f'font-weight:700;">{s.get("jobs", 0)}</td>'
            f'<td style="padding:14px 0;border-top:1px solid #e5e7eb;text-align:center;'
            f'font-weight:700;">{s.get("email", 0)}</td>'
            f'<td style="padding:14px 0;border-top:1px solid #e5e7eb;text-align:center;'
            f'font-weight:700;">{s.get("phone", 0)}</td>'
            f'<td style="padding:14px 0;border-top:1px solid #e5e7eb;text-align:center;'
            f'font-weight:700;">{s.get("linkedin", 0)}</td>'
            f'</tr>'
        )

    # Total row
    source_rows += (
        f'<tr>'
        f'<td style="padding:16px 0 0;border-top:2px solid #e5e7eb;font-weight:800;'
        f'color:#6b7280;">Total</td>'
        f'<td style="padding:16px 0 0;border-top:2px solid #e5e7eb;text-align:center;'
        f'font-weight:800;">{total_jobs}</td>'
        f'<td style="padding:16px 0 0;border-top:2px solid #e5e7eb;text-align:center;'
        f'font-weight:800;">{with_email}</td>'
        f'<td style="padding:16px 0 0;border-top:2px solid #e5e7eb;text-align:center;'
        f'font-weight:800;">{with_phone}</td>'
        f'<td style="padding:16px 0 0;border-top:2px solid #e5e7eb;text-align:center;'
        f'font-weight:800;">{with_linkedin}</td>'
        f'</tr>'
    )

    # ── Naukri detail ────────────────────────────────────────────────────
    nk = ps.get("Naukri", {})
    naukri_html = ""
    if nk.get("jobs"):
        nk_jobs = nk["jobs"]

        def _nk_card(label: str, val: int, color: str) -> str:
            rate = _pct(val, nk_jobs)
            return (
                f'<td style="padding:6px;">'
                f'<div style="background:#fff;border-radius:8px;padding:14px 16px;'
                f'display:flex;align-items:baseline;justify-content:space-between;">'  # noqa: E501
                f'<span style="font-size:12px;color:#6b7280;">{label}</span>'
                f'<span><span style="font-size:20px;font-weight:900;color:{color};">'
                f'{val}</span>'
                f'<span style="font-size:10px;font-weight:600;color:#6b7280;'
                f'margin-left:3px;">{rate}</span></span></div></td>'
            )

        naukri_html = (
            f'<div style="padding:24px 32px;border-top:1px solid #e5e7eb;">'
            f'<div style="font-size:11px;font-weight:700;letter-spacing:0.1em;'
            f'text-transform:uppercase;color:#6b7280;margin:0 0 16px;">Naukri Source Detail</div>'
            f'<div style="background:#eef2ff;border-radius:12px;padding:20px 18px;">'
            f'<div style="font-size:13px;font-weight:800;color:#6366f1;margin:0 0 14px;">'
            f'Scraping Extraction &middot; {nk_jobs} jobs</div>'
            f'<table style="width:100%;border-collapse:collapse;"><tr>'
            f'{_nk_card("Email scraped", nk.get("email", 0), "#0ea5e9")}'
            f'{_nk_card("Phone scraped", nk.get("phone", 0), "#6366f1")}'
            f'</tr><tr>'
            f'{_nk_card("LinkedIn profile", nk.get("linkedin", 0), "#10b981")}'
            f'{_nk_card("Poster name", nk.get("poster_name", 0), "#1a1d26")}'
            f'</tr></table></div></div>'
        )

    # ── Apollo credits ───────────────────────────────────────────────────
    apollo_html = ""
    ad = apollo_daily or {}
    am = apollo_monthly or {}
    daily_used = ad.get("global_used", 0)
    daily_cap = ad.get("global_cap", 0)
    monthly_used = am.get("monthly_used", 0)
    monthly_cap = am.get("monthly_cap", 0)
    monthly_remaining = am.get("monthly_remaining")

    if apollo or daily_used or monthly_used:
        daily_label = f"{daily_used} / {daily_cap}" if daily_cap and daily_cap > 0 else str(daily_used)
        monthly_rem_label = (
            f"{monthly_remaining} / {monthly_cap}"
            if monthly_remaining is not None and monthly_cap and monthly_cap > 0
            else "Unlimited"
        )
        monthly_pct = (
            round(monthly_used * 100 / monthly_cap) if monthly_cap and monthly_cap > 0 else 0
        )
        monthly_bar_color = "#f59e0b" if monthly_pct < 80 else "#ef4444"

        apollo_html = (
            f'<div style="padding:24px 32px;border-top:1px solid #e5e7eb;">'
            f'<div style="font-size:11px;font-weight:700;letter-spacing:0.1em;'
            f'text-transform:uppercase;color:#6b7280;margin:0 0 16px;">Apollo Credits</div>'
            f'<table style="width:100%;border-collapse:collapse;">'
            # Row 1: This Run + Today
            f'<tr>'
            f'<td style="padding:8px 12px;width:50%;vertical-align:top;">'
            f'<div style="background:#fffbeb;border-radius:10px;padding:16px 18px;">'
            f'<div style="font-size:11px;color:#6b7280;font-weight:600;'
            f'text-transform:uppercase;letter-spacing:0.04em;">This Run</div>'
            f'<div style="font-size:28px;font-weight:900;color:#f59e0b;line-height:1.2;">'
            f'{apollo}</div></div></td>'
            f'<td style="padding:8px 12px;width:50%;vertical-align:top;">'
            f'<div style="background:#fef3c7;border-radius:10px;padding:16px 18px;">'
            f'<div style="font-size:11px;color:#6b7280;font-weight:600;'
            f'text-transform:uppercase;letter-spacing:0.04em;">Used Today</div>'
            f'<div style="font-size:28px;font-weight:900;color:#d97706;line-height:1.2;">'
            f'{daily_label}</div></div></td>'
            f'</tr>'
            # Row 2: Monthly remaining
            f'<tr><td colspan="2" style="padding:8px 12px;">'
            f'<div style="background:#f0fdf4;border-radius:10px;padding:16px 18px;">'
            f'<div style="display:flex;justify-content:space-between;align-items:baseline;">'
            f'<span style="font-size:11px;color:#6b7280;font-weight:600;'
            f'text-transform:uppercase;letter-spacing:0.04em;">Monthly Remaining</span>'
            f'<span style="font-size:20px;font-weight:900;color:#059669;">'
            f'{monthly_rem_label}</span></div>'
        )
        if monthly_cap and monthly_cap > 0:
            apollo_html += (
                f'<div style="height:8px;border-radius:4px;background:#e5e7eb;'
                f'margin-top:10px;overflow:hidden;">'
                f'<div style="height:100%;width:{min(monthly_pct, 100)}%;border-radius:4px;'
                f'background:{monthly_bar_color};"></div></div>'
                f'<div style="font-size:11px;color:#6b7280;margin-top:4px;">'
                f'{monthly_used} of {monthly_cap} credits used this month ({monthly_pct}%)</div>'
            )
        apollo_html += '</div></td></tr></table></div>'

    # ── Coverage gauges ──────────────────────────────────────────────────
    email_pct = round(with_email * 100 / total) if total else 0
    phone_pct = round(with_phone * 100 / total) if total else 0
    li_pct = round(with_linkedin * 100 / total) if total else 0

    def _gauge(label: str, pct: int, count: int, color: str) -> str:
        return (
            f'<td style="padding:0 8px;vertical-align:top;">'
            f'<div style="display:flex;justify-content:space-between;align-items:baseline;'
            f'margin-bottom:6px;">'
            f'<span style="font-size:13px;font-weight:600;">{label}</span>'
            f'<span style="font-size:18px;font-weight:900;color:{color};">{pct}%</span></div>'
            f'<div style="height:8px;border-radius:4px;background:#f1f5f9;overflow:hidden;">'
            f'<div style="height:100%;width:{pct}%;border-radius:4px;background:{color};"></div>'
            f'</div>'
            f'<div style="font-size:11px;color:#6b7280;margin-top:3px;">'
            f'{count} of {total_jobs} jobs</div></td>'
        )

    # ── Assemble ─────────────────────────────────────────────────────────
    return (
        # Outer wrapper
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,'
        '\'Helvetica Neue\',Arial,sans-serif;font-size:14px;line-height:1.6;color:#1a1d26;'
        'max-width:600px;margin:0 auto;">'

        # Gradient top bar
        '<div style="height:6px;background:linear-gradient(135deg,#0ea5e9,#8b5cf6);'
        'border-radius:12px 12px 0 0;"></div>'

        # Card
        '<div style="background:#fff;border:1px solid #e5e7eb;border-top:none;'
        'border-radius:0 0 12px 12px;overflow:hidden;">'

        # Header
        '<div style="padding:28px 32px 0;">'
        '<div style="font-size:11px;font-weight:700;letter-spacing:0.1em;'
        'text-transform:uppercase;color:#6b7280;margin-bottom:8px;">Sightspectrum</div>'
        '<div style="font-size:24px;font-weight:800;letter-spacing:-0.03em;margin:0;">'
        'Harvest Run Report</div>'
        f'<div style="font-size:13px;color:#6b7280;margin-top:4px;">'
        f'{_esc(run_date)} &middot; {src_str} '
        f'<span style="display:inline-block;font-size:11px;font-weight:700;'
        f'letter-spacing:0.04em;text-transform:uppercase;padding:3px 10px;'
        f'border-radius:100px;background:{status_color};color:#fff;margin-left:6px;'
        f'vertical-align:middle;">{_esc(status)}</span></div></div>'

        # Hero
        '<div style="text-align:center;padding:32px 32px 28px;">'
        '<div style="font-size:64px;font-weight:900;letter-spacing:-0.04em;line-height:1;'
        'color:#0ea5e9;">'
        f'{_esc(total_jobs)}</div>'
        '<div style="font-size:13px;font-weight:600;letter-spacing:0.06em;'
        'text-transform:uppercase;color:#6b7280;margin-top:6px;">Jobs Harvested</div></div>'

        # Stat pills
        '<div style="text-align:center;padding:0 32px 24px;">'
        f'<span style="display:inline-block;background:#f9fafb;border:1px solid #e5e7eb;'
        f'border-radius:100px;padding:8px 18px;margin:4px;font-size:13px;">'
        f'<b>{_esc(duration)}</b> <span style="color:#6b7280;">Duration</span></span>'
        f'<span style="display:inline-block;background:#f9fafb;border:1px solid #e5e7eb;'
        f'border-radius:100px;padding:8px 18px;margin:4px;font-size:13px;">'
        f'<b>{len(sources)}</b> <span style="color:#6b7280;">Sources</span></span>'
        f'<span style="display:inline-block;background:#f9fafb;border:1px solid #e5e7eb;'
        f'border-radius:100px;padding:8px 18px;margin:4px;font-size:13px;">'
        f'<b>{with_contact}</b> <span style="color:#6b7280;">Contactable</span></span>'
        f'<span style="display:inline-block;background:#f9fafb;border:1px solid #e5e7eb;'
        f'border-radius:100px;padding:8px 18px;margin:4px;font-size:13px;">'
        f'<b>{apollo}</b> <span style="color:#6b7280;">Apollo Credits</span></span></div>'

        # Divider
        '<div style="height:1px;background:#e5e7eb;margin:0 32px;"></div>'

        # Run details
        '<div style="padding:24px 32px;">'
        '<div style="font-size:11px;font-weight:700;letter-spacing:0.1em;'
        'text-transform:uppercase;color:#6b7280;margin:0 0 16px;">Run Details</div>'
        '<table style="width:100%;border-collapse:collapse;">'
        '<tr>'
        f'<td style="padding:0 0 14px;width:50%;vertical-align:top;">'
        f'<div style="font-size:11px;color:#6b7280;font-weight:600;letter-spacing:0.03em;'
        f'text-transform:uppercase;">Run Date</div>'
        f'<div style="font-size:15px;font-weight:700;margin-top:1px;">{_esc(run_date)}</div></td>'
        f'<td style="padding:0 0 14px;width:50%;vertical-align:top;">'
        f'<div style="font-size:11px;color:#6b7280;font-weight:600;letter-spacing:0.03em;'
        f'text-transform:uppercase;">Sources</div>'
        f'<div style="font-size:15px;font-weight:700;margin-top:1px;">{src_str}</div></td></tr>'
        '<tr>'
        f'<td style="padding:0 0 14px;width:50%;vertical-align:top;">'
        f'<div style="font-size:11px;color:#6b7280;font-weight:600;letter-spacing:0.03em;'
        f'text-transform:uppercase;">Start Time</div>'
        f'<div style="font-size:15px;font-weight:700;margin-top:1px;">{_esc(started)}</div></td>'
        f'<td style="padding:0 0 14px;width:50%;vertical-align:top;">'
        f'<div style="font-size:11px;color:#6b7280;font-weight:600;letter-spacing:0.03em;'
        f'text-transform:uppercase;">End Time</div>'
        f'<div style="font-size:15px;font-weight:700;margin-top:1px;">{_esc(completed)}</div></td>'
        '</tr></table></div>'

        # Divider
        '<div style="height:1px;background:#e5e7eb;margin:0 32px;"></div>'

        # Source breakdown
        '<div style="padding:24px 32px;">'
        '<div style="font-size:11px;font-weight:700;letter-spacing:0.1em;'
        'text-transform:uppercase;color:#6b7280;margin:0 0 16px;">Source Breakdown</div>'
        '<table style="width:100%;border-collapse:collapse;">'
        '<tr>'
        '<th style="font-size:10px;font-weight:700;letter-spacing:0.08em;'
        'text-transform:uppercase;color:#6b7280;text-align:left;padding:0 0 10px;">Source</th>'
        '<th style="font-size:10px;font-weight:700;letter-spacing:0.08em;'
        'text-transform:uppercase;color:#6b7280;text-align:center;padding:0 0 10px;">Jobs</th>'
        '<th style="font-size:10px;font-weight:700;letter-spacing:0.08em;'
        'text-transform:uppercase;color:#6b7280;text-align:center;padding:0 0 10px;">Email</th>'
        '<th style="font-size:10px;font-weight:700;letter-spacing:0.08em;'
        'text-transform:uppercase;color:#6b7280;text-align:center;padding:0 0 10px;">Phone</th>'
        '<th style="font-size:10px;font-weight:700;letter-spacing:0.08em;'
        'text-transform:uppercase;color:#6b7280;text-align:center;padding:0 0 10px;">LinkedIn</th>'
        '</tr>'
        f'{source_rows}</table>'

        # Mini coverage bar
        f'<div style="height:4px;border-radius:2px;background:#e5e7eb;margin-top:12px;'
        f'overflow:hidden;">'
        f'<div style="height:100%;width:{email_pct}%;border-radius:2px;'
        f'background:#0ea5e9;"></div></div>'
        f'<div style="display:flex;justify-content:space-between;font-size:11px;'
        f'color:#6b7280;margin-top:4px;">'
        f'<span>{email_pct}% with email</span>'
        f'<span>{phone_pct}% with phone</span>'
        f'<span>{li_pct}% with LinkedIn</span></div></div>'

        # Naukri detail
        f'{naukri_html}'

        # Apollo credits
        f'{apollo_html}'

        # Contact coverage gauges
        '<div style="padding:24px 32px;border-top:1px solid #e5e7eb;">'
        '<div style="font-size:11px;font-weight:700;letter-spacing:0.1em;'
        'text-transform:uppercase;color:#6b7280;margin:0 0 16px;">Contact Coverage</div>'
        '<table style="width:100%;border-collapse:collapse;"><tr>'
        f'{_gauge("Email", email_pct, with_email, "#0ea5e9")}'
        f'{_gauge("Phone", phone_pct, with_phone, "#6366f1")}'
        f'{_gauge("LinkedIn", li_pct, with_linkedin, "#10b981")}'
        '</tr></table></div>'

        # Footer
        '<div style="padding:20px 32px;border-top:1px solid #e5e7eb;text-align:center;'
        'font-size:12px;color:#6b7280;">'
        'The detailed data is attached as Excel.<br>Sightspectrum Harvest Agent</div>'

        '</div></div>'
    )


def _render_failed_html(run_id: str, status: str, src_str: str, error: str) -> str:
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,'
        '\'Helvetica Neue\',Arial,sans-serif;font-size:14px;line-height:1.6;color:#1a1d26;'
        'max-width:600px;margin:0 auto;">'

        '<div style="height:6px;background:linear-gradient(135deg,#ef4444,#f59e0b);'
        'border-radius:12px 12px 0 0;"></div>'

        '<div style="background:#fff;border:1px solid #e5e7eb;border-top:none;'
        'border-radius:0 0 12px 12px;overflow:hidden;">'

        '<div style="padding:28px 32px;">'
        '<div style="font-size:11px;font-weight:700;letter-spacing:0.1em;'
        'text-transform:uppercase;color:#6b7280;margin-bottom:8px;">Sightspectrum</div>'
        f'<div style="font-size:22px;font-weight:800;letter-spacing:-0.02em;margin:0;">'
        f'Harvest Run Failed</div>'
        f'<div style="font-size:13px;color:#6b7280;margin-top:4px;">Run {_esc(run_id)}</div></div>'

        '<div style="padding:0 32px 24px;">'
        f'<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:12px;'
        f'padding:20px 24px;">'
        f'<div style="font-size:13px;font-weight:700;color:#dc2626;margin-bottom:8px;">'
        f'Error details</div>'
        f'<div style="font-size:14px;color:#991b1b;">{_esc(error or "unknown")}</div></div>'
        f'<div style="margin-top:16px;font-size:14px;color:#6b7280;">'
        f'Sources: {src_str}<br>'
        f'No report is attached — the run did not reach the export step.</div></div>'

        '<div style="padding:20px 32px;border-top:1px solid #e5e7eb;text-align:center;'
        'font-size:12px;color:#6b7280;">Sightspectrum Harvest Agent</div>'

        '</div></div>'
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
    run_timing: dict | None = None,
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

    apollo_daily = apollo_monthly = None
    if status != "failed":
        try:
            apollo_daily = await usage_today()
            apollo_monthly = await usage_this_month()
        except Exception:
            log.warning("harvest_report_apollo_usage_fetch_failed")

    body = _render_body(run_id, status, total_jobs, sources, error, insights, run_timing)
    html_body = _render_body_html(
        run_id, status, total_jobs, sources, error, insights, run_timing,
        apollo_daily, apollo_monthly,
    )

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
