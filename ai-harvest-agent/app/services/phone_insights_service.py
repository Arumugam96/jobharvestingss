"""
Phone outreach insights email — sent 24 hours after a harvest run completes.
Derives call activity from the phone_call_logs table for jobs in the run.

Delivery: ``send_due_phone_insights`` (APScheduler interval job registered in
app/main.py, every 30 min) sends it for every multi-source run that completed 24–72h
ago and has no ``harvest_runs.phone_insights_sent_at`` yet — so it survives a restart
(APScheduler's in-memory job store does not). The one-shot scheduled at run end is
kept as a fast path; both go through the same atomic claim on
``phone_insights_sent_at``, so a run's email is sent exactly once.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import re

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.harvest_run import HarvestRunORM, ScrapedJobORM
from app.models.phone_call_log import PhoneCallLogORM
from app.models.recruiter import RecruiterORM
from app.services.email_service import EmailSender, report_identity_from
from app.services.harvest_run_service import _contact_presence_exprs, _txt_expr, db_read, db_write

logger = structlog.get_logger(__name__)

# Call-log phone_source → report row. "contact" is the UI's merged poster value
# (older rows with no job/recruiter split) — it is the job poster's number.
_SOURCE_ROWS = (
    ("job", "Job Posting", ("job", "contact")),
    ("recruiter", "Recruiter (Apollo)", ("recruiter",)),
)
_DONE_STATUSES = ("success", "completed", "no_results")

_DIGITS_RE = re.compile(r"\D")


def _norm_phone(raw: str | None) -> str:
    """Strip a phone string to its trailing 10 digits for dedup comparison."""
    if not raw:
        return ""
    digits = _DIGITS_RE.sub("", raw.strip())
    return digits[-10:] if len(digits) >= 10 else digits


def _empty(run_id: str) -> dict[str, Any]:
    return {"total_phones": 0, "called": 0, "not_called": 0, "answered": 0, "unanswered": 0,
            "called_pct": 0, "not_called_pct": 0, "sources": [], "run_id": run_id}


async def compute_phone_outreach_insights(
    db: AsyncSession, run_id: str, tenant_id: str,
) -> dict[str, Any]:
    """Compute phone outreach insights for a completed harvest run.

    ``run_id`` is the harvest_runs surrogate PK (UUID), which is what
    ScrapedJobORM.run_id references as an FK.

    A job "has a phone" exactly as the jobs table shows it — its scraped number, its
    recruiter's (incl. Apollo-delivered) number, or its company HR contact's
    (``_contact_presence_exprs()["mobile"]``). Call activity uses the LATEST
    non-cleared call per job (same rule as the Phone Tracker badge), counted only for
    jobs that have a phone — so called + not_called == total_phones.
    """
    J, R = ScrapedJobORM, RecruiterORM
    mobile = _contact_presence_exprs()["mobile"]
    base = (
        select(J.id, _txt_expr(J.contact_number).label("job_phone"),
               _txt_expr(R.contact_number).label("rec_phone"),
               _txt_expr(J.company_contact_phone).label("co_phone"))
        .select_from(J).outerjoin(R, J.recruiter_id == R.id)
        .where(J.run_id == run_id, J.tenant_id == tenant_id, mobile)
    )
    rows = (await db.execute(base)).all()
    if not rows:
        return _empty(run_id)
    phone_jobs = {r.id for r in rows}

    # Deduplicate by normalised phone string. Each unique number is attributed to
    # exactly one source using priority: job > recruiter > company (the first
    # source that produced this number). This keeps the per-source rows from
    # overlapping, so their sum equals total_phones.
    seen_phones: dict[str, str] = {}  # norm_phone → source key
    phone_to_jobs: dict[str, set[str]] = {}  # norm_phone → set of job ids
    for r in rows:
        for phone_str, src_key in (
            (r.job_phone, "job"),
            (r.rec_phone, "recruiter"),
            (r.co_phone, "company"),
        ):
            norm = _norm_phone(phone_str)
            if not norm:
                continue
            phone_to_jobs.setdefault(norm, set()).add(r.id)
            if norm not in seen_phones:
                seen_phones[norm] = src_key

    total_phones = len(seen_phones)
    if total_phones == 0:
        return _empty(run_id)

    per_source = {"job": 0, "recruiter": 0, "company": 0}
    for src_key in seen_phones.values():
        per_source[src_key] += 1

    # Latest non-cleared call per job (ids are autoincrement → max(id) is newest).
    C = PhoneCallLogORM
    newest = (
        select(func.max(C.id))
        .where(C.job_id.in_(list(phone_jobs)), C.tenant_id == tenant_id, C.cleared_at.is_(None))
        .group_by(C.job_id)
    )
    latest = (await db.execute(select(C).where(C.id.in_(newest.scalar_subquery())))).scalars().all()
    latest = [c for c in latest if c.job_id in phone_jobs]

    # Deduplicate call logs by normalised phone: one call per unique number.
    called_phones: set[str] = set()
    answered_phones: set[str] = set()
    unanswered_phones: set[str] = set()
    call_by_phone: dict[str, Any] = {}  # norm_phone → call log row (for source attribution)
    for c in latest:
        norm = _norm_phone(c.phone_number) if hasattr(c, "phone_number") else ""
        if not norm:
            for p, jids in phone_to_jobs.items():
                if c.job_id in jids:
                    norm = p
                    break
        if not norm:
            continue
        if norm in called_phones:
            continue
        called_phones.add(norm)
        call_by_phone[norm] = c
        if c.call_status == "answered":
            answered_phones.add(norm)
        elif c.call_status == "unanswered":
            unanswered_phones.add(norm)

    called = len(called_phones)
    not_called = max(0, total_phones - called)
    answered = len(answered_phones)
    unanswered = len(unanswered_phones)

    sources = []
    for key, label, call_sources in _SOURCE_ROWS:
        count = per_source[key]
        src_called = sum(
            1 for p, sk in seen_phones.items()
            if sk == key and p in called_phones
        )
        ans = sum(
            1 for p, sk in seen_phones.items()
            if sk == key and p in answered_phones
        )
        unans = sum(
            1 for p, sk in seen_phones.items()
            if sk == key and p in unanswered_phones
        )
        sources.append({
            "label": label,
            "count": count,
            "called": src_called,
            "answered": ans,
            "unanswered": unans,
            "coverage": round(src_called / count * 100, 1) if count > 0 else 0,
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


def _src_badge(name: str) -> str:
    """Reuse the shared source icon badge from harvest_notification_service."""
    from app.services.harvest_notification_service import _src_badge as _shared_badge
    return _shared_badge(name)


def _funnel_seg(color: str, count: int, total: int, label: str) -> str:
    """One segment of the outreach funnel bar (email-safe table cell)."""
    if total == 0 or count == 0:
        return ""
    pct = round(count * 100 / total, 1)
    width_pct = max(pct, 8)  # min 8% so label is readable
    return (
        f'<td bgcolor="{color}" width="{width_pct}%" '
        f'style="background-color:{color};height:32px;color:#ffffff;'
        f'font-size:12px;font-weight:700;text-align:center;line-height:32px;'
        f'white-space:nowrap;" align="center" valign="middle">'
        f'{count}</td>'
    )


def _legend_dot(color: str, label: str) -> str:
    """Funnel legend item — coloured square + text, email-safe inline-table."""
    return (
        f'<table cellpadding="0" cellspacing="0" border="0" '
        f'style="display:inline-table;border-collapse:collapse;"><tr>'
        f'<td style="width:10px;height:10px;background-color:{color};'
        f'border-radius:2px;" width="10" height="10">&nbsp;</td>'
        f'<td style="padding:0 0 0 5px;font-size:12px;color:#64748B;'
        f'white-space:nowrap;">{label}</td>'
        f'</tr></table>'
    )


def render_phone_insights_email_html(
    insights: dict[str, Any], run_label: str, sources: list[str] | None = None,
) -> str:
    """Render the phone outreach insights as an HTML email with inline CSS.

    Design: 5 KPI cards, outreach funnel bar, expanded source coverage table
    with Answered / Not Answered columns. All table-based + inline CSS for
    Outlook / Gmail compatibility.
    """
    total = insights["total_phones"]
    called = insights["called"]
    not_called = insights["not_called"]
    answered = insights["answered"]
    unanswered = insights["unanswered"]
    called_pct = insights["called_pct"]
    not_called_pct = insights["not_called_pct"]
    source_rows_data = insights["sources"]

    ans_of_out = round(answered * 100 / called, 1) if called else 0
    unans_of_out = round(unanswered * 100 / called, 1) if called else 0

    # ── KPI card helper (Outlook-safe: table cell with bgcolor) ─────────
    def _kpi(value, label: str, color: str, sub: str = "") -> str:
        sub_html = (
            f'<div style="font-size:12px;font-weight:600;color:{color};'
            f'margin-top:3px">{sub}</div>' if sub else ""
        )
        return (
            f'<td bgcolor="#F8FAFC" style="background-color:#F8FAFC;'
            f'border:1px solid #E2E8F0;padding:14px 10px;text-align:center" '
            f'align="center" valign="top">'
            f'<div style="font-size:30px;font-weight:700;color:{color};'
            f'line-height:1.1">{value}</div>'
            f'<div style="font-size:11px;color:#64748B;margin-top:5px;'
            f'line-height:1.3">{label}</div>{sub_html}</td>'
        )

    # ── Source rows ─────────────────────────────────────────────────────
    _TH = (
        'style="text-align:{align};font-size:11px;font-weight:600;'
        'letter-spacing:.05em;text-transform:uppercase;color:#64748B;'
        'padding:10px 10px;border-bottom:1px solid #E2E8F0"'
    )
    _TD = (
        'style="padding:10px 10px;border-bottom:1px solid #E2E8F0;'
        'text-align:{align};font-size:13px;{extra}"'
    )

    source_rows = ""
    total_count = total_called = total_ans = total_unans = 0
    for s in source_rows_data:
        cov_color = "#16A34A" if s["coverage"] >= 30 else "#D97706"
        s_ans = s.get("answered", 0)
        s_unans = s.get("unanswered", 0)
        total_count += s["count"]
        total_called += s["called"]
        total_ans += s_ans
        total_unans += s_unans
        source_rows += (
            f'<tr>'
            f'<td {_TD.format(align="left", extra="font-weight:600")}>{s["label"]}</td>'
            f'<td {_TD.format(align="center", extra="")}>{s["count"]}</td>'
            f'<td {_TD.format(align="center", extra="")}>{s["called"]}</td>'
            f'<td {_TD.format(align="center", extra="color:#16A34A;font-weight:600")}>{s_ans}</td>'
            f'<td {_TD.format(align="center", extra="color:#DC2626;font-weight:600")}>{s_unans}</td>'
            f'<td {_TD.format(align="center", extra="")}>'
            f'<span style="font-weight:700;color:{cov_color}">{s["coverage"]}%</span></td>'
            f'</tr>'
        )
    # Total row
    total_cov = round(total_called * 100 / total_count, 1) if total_count else 0
    total_cov_color = "#16A34A" if total_cov >= 30 else "#D97706"
    source_rows += (
        f'<tr>'
        f'<td {_TD.format(align="left", extra="font-weight:800;border-top:2px solid #CBD5E1")}>Total</td>'
        f'<td {_TD.format(align="center", extra="font-weight:800;border-top:2px solid #CBD5E1")}>{total_count}</td>'
        f'<td {_TD.format(align="center", extra="font-weight:800;border-top:2px solid #CBD5E1")}>{total_called}</td>'
        f'<td {_TD.format(align="center", extra="font-weight:800;color:#16A34A;border-top:2px solid #CBD5E1")}>{total_ans}</td>'
        f'<td {_TD.format(align="center", extra="font-weight:800;color:#DC2626;border-top:2px solid #CBD5E1")}>{total_unans}</td>'
        f'<td {_TD.format(align="center", extra="font-weight:800;border-top:2px solid #CBD5E1")}>'
        f'<span style="color:{total_cov_color}">{total_cov}%</span></td>'
        f'</tr>'
    )

    # ── Funnel bar segments ─────────────────────────────────────────────
    funnel = (
        _funnel_seg("#16A34A", answered, total, "Answered")
        + _funnel_seg("#DC2626", unanswered, total, "Not Answered")
        + _funnel_seg("#F59E0B", not_called, total, "Not Outreached")
    )

    # ── Source badges in header ─────────────────────────────────────────
    src_badges = ""
    if sources:
        src_badges = "".join(_src_badge(s) for s in sources) + " "

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#F1F5F9;font-family:Arial,Helvetica,sans-serif;color:#1E293B">
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:620px;margin:0 auto;background:#ffffff">
  <!-- Header -->
  <tr>
    <td bgcolor="#2563EB" style="background-color:#2563EB;padding:28px 32px 24px;text-align:center">
      <h1 style="margin:0;font-size:22px;font-weight:700;color:#ffffff;letter-spacing:-.02em">Phone Outreach Insights</h1>
      <div style="display:inline-block;margin-top:10px;background:rgba(255,255,255,.15);color:#fff;font-size:11px;font-weight:600;padding:3px 14px;border-radius:12px;letter-spacing:.02em;line-height:28px">{src_badges}<span style="vertical-align:middle;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;letter-spacing:.05em;opacity:.85">{run_label}</span></div>
    </td>
  </tr>
  <!-- Overview KPIs — 3 + 2 rows for Outlook compatibility -->
  <tr>
    <td style="padding:24px 24px 0">
      <div style="font-size:14px;font-weight:700;color:#1E293B;margin:0 0 14px;padding-bottom:8px;border-bottom:2px solid #2563EB;display:inline-block">Overview</div>
      <table width="100%" cellpadding="0" cellspacing="6" border="0">
        <tr>
          {_kpi(total, "Total Phones", "#2563EB")}
          {_kpi(called, "Outreached", "#16A34A", f"{called_pct}%")}
          {_kpi(not_called, "Not Outreached", "#D97706", f"{not_called_pct}%")}
        </tr>
        <tr>
          {_kpi(answered, "Answered", "#16A34A", f"{ans_of_out}% of outreached")}
          {_kpi(unanswered, "Not Answered", "#DC2626", f"{unans_of_out}% of outreached")}
          <td></td>
        </tr>
      </table>
    </td>
  </tr>
  <!-- Outreach Funnel -->
  <tr>
    <td style="padding:14px 24px 0">
      <div style="font-size:14px;font-weight:700;color:#1E293B;margin:0 0 14px;padding-bottom:8px;border-bottom:2px solid #2563EB;display:inline-block">Outreach Funnel</div>
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;border-radius:8px;overflow:hidden">
        <tr>{funnel}</tr>
      </table>
      <div style="padding:10px 0 0;text-align:center">
        {_legend_dot("#16A34A", f"Answered {answered}")}
        &nbsp;&nbsp;
        {_legend_dot("#DC2626", f"Not Answered {unanswered}")}
        &nbsp;&nbsp;
        {_legend_dot("#F59E0B", f"Not Outreached {not_called}")}
      </div>
    </td>
  </tr>
  <!-- Phone Source Coverage -->
  <tr>
    <td style="padding:20px 24px 24px">
      <div style="font-size:14px;font-weight:700;color:#1E293B;margin:0 0 14px;padding-bottom:8px;border-bottom:2px solid #2563EB;display:inline-block">Phone Source Coverage</div>
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;font-size:13px">
        <thead>
          <tr>
            <th {_TH.format(align="left")}>Source</th>
            <th {_TH.format(align="center")}>Count</th>
            <th {_TH.format(align="center")}>Called</th>
            <th {_TH.format(align="center")}>Answered</th>
            <th {_TH.format(align="center")}>Not Ans.</th>
            <th {_TH.format(align="center")}>Coverage</th>
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


async def _claim(run_id: str) -> bool:
    """Atomically stamp phone_insights_sent_at (only if still NULL). True when this
    caller won the claim — the one-shot and the sweep can never both send."""
    async def _w(db: AsyncSession) -> bool:
        H = HarvestRunORM
        res = await db.execute(
            update(H).where(H.id == run_id, H.phone_insights_sent_at.is_(None))
            .values(phone_insights_sent_at=datetime.now(timezone.utc))
        )
        return (res.rowcount or 0) > 0
    return bool(await db_write(_w))


async def _release(run_id: str) -> None:
    """Undo a claim after a failed send so the sweep retries it."""
    async def _w(db: AsyncSession) -> None:
        H = HarvestRunORM
        await db.execute(update(H).where(H.id == run_id).values(phone_insights_sent_at=None))
    await db_write(_w)


async def send_phone_outreach_insights(
    run_id: str, tenant_id: str, run_display_id: str, sources_label: str,
    sources: list[str] | None = None,
) -> bool:
    """Compute + send the phone outreach insights email for a completed harvest run,
    at most once per run (claims ``phone_insights_sent_at`` first). Returns True when
    an email went out. Never raises.

    ``run_id`` is the harvest_runs surrogate PK (UUID).
    ``run_display_id`` is the human-readable timestamp id (e.g. 20261105_143000).
    """
    if not await _claim(run_id):
        logger.info("phone_insights_skip_already_sent", run_id=run_id)
        return False
    try:
        settings = get_settings()

        result = await db_read(
            lambda db: compute_phone_outreach_insights(db, run_id, tenant_id)
        )
        if result is None:
            await _release(run_id)  # DB read failed — let the sweep retry
            return False
        if result["total_phones"] == 0:
            logger.warning(
                "phone_insights_skip_no_phones", run_id=run_id, tenant_id=tenant_id,
                hint="Run has zero phone contacts — email permanently skipped",
            )
            return False  # claim kept: nothing to report for this run, ever

        run_label = run_display_id
        html = render_phone_insights_email_html(result, run_label, sources=sources)

        sender = EmailSender(settings)
        subject = (
            f"\U0001f4de Phone Outreach Insights — "
            f"24h after {sources_label} Harvest ({run_display_id})"
        )

        # Send to the same recipients as the harvest report
        from app.services.config_service import ConfigService
        config = ConfigService().load()
        recipients = (
            config.notifications.recipients if config.notifications else []
        )
        if not recipients:
            recipients = [settings.outreach_auto_reply_to or settings.smtp_sender_mail]

        sender_identity = report_identity_from(settings, "JobHarvest Phone Insights")

        from app.services.harvest_notification_service import collect_source_icon_attachments
        icon_atts = collect_source_icon_attachments(html)
        await sender.send_email_with_attachments(
            recipients,
            subject,
            "Phone outreach insights are available — view the HTML version of this email.",
            html_body=html,
            sender_identity=sender_identity,
            inline_attachments=icon_atts,
        )
        logger.info("phone_insights_sent", run_id=run_id, recipients=len(recipients))
        return True
    except Exception as exc:
        logger.warning("phone_insights_failed", run_id=run_id, error=str(exc))
        await _release(run_id)
        return False


async def due_runs(db: AsyncSession, *, now: datetime | None = None) -> list[HarvestRunORM]:
    """Multi-source runs that completed 24–72h ago and haven't had their insights sent."""
    now = now or datetime.now(timezone.utc)
    H = HarvestRunORM
    return list((await db.execute(
        select(H).where(
            H.status.in_(_DONE_STATUSES), H.source.is_(None),
            H.phone_insights_sent_at.is_(None),
            H.completed_at <= now - timedelta(hours=24),
            H.completed_at > now - timedelta(hours=72),
        ).order_by(H.completed_at.asc())
    )).scalars().all())


async def send_due_phone_insights() -> int:
    """APScheduler tick (every 30 min): send the 24h insights email for every due run.
    Restart-safe (state lives on harvest_runs). Returns emails sent. Never raises."""
    try:
        runs = await db_read(lambda db: due_runs(db)) or []
    except Exception as exc:  # db_read already swallows — belt and braces
        logger.warning("phone_insights_sweep_failed", error=str(exc))
        return 0
    sent = 0
    for run in runs:
        label = " + ".join(run.sources or []) or "Harvest"
        if await send_phone_outreach_insights(
            run_id=run.id, tenant_id=run.tenant_id, run_display_id=run.run_id,
            sources_label=label, sources=run.sources,
        ):
            sent += 1
    if runs:
        logger.info("phone_insights_sweep_done", due=len(runs), sent=sent)
    return sent
