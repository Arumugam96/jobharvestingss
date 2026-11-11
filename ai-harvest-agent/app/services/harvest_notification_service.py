"""
Sends the post-harvest report email (Excel/JSON attachments) once a harvest
run completes — called from both the manual (POST /run-harvest-agent) and
scheduled (APScheduler auto-run) completion paths.

The attachments are generated in memory from the run's job records (with
each job's RecruiterORM contact info already merged in — see
app/services/report_service.py); no result file is written to disk anymore.

Everything is driven by HarvestConfig.notifications (harvest_config.json):
enable/disable, recipient list, sender display name (email_cover_label), which
formats to attach (excel/json/both/None) and the subject line template — no code
change needed to retarget or restyle.

The HTML body is built for Outlook (Word rendering engine) as well as web/mobile
clients: table-only layout, padding only on cells, every background set via the
``bgcolor`` attribute AND inline ``background-color``, no flexbox / gap / overflow.
"""
from __future__ import annotations

import html as html_lib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

from app.models.harvest_config import NotificationConfig
from app.services.apollo_budget import usage_this_month, usage_today
from app.services.email_service import EmailSender, report_identity_from
from app.services.report_service import build_excel_report_bytes, build_json_report_bytes

logger = structlog.get_logger(__name__)

_DEFAULT_SUBJECT = "Harvest Report — {run_id} ({status}, {total_jobs} jobs)"

# Report times are shown in IST (the business's timezone); naive datetimes are
# assumed to be UTC, like every timestamp the harvest pipeline produces.
_IST = timezone(timedelta(hours=5, minutes=30), "IST")


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds or 0), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s" if m else f"{s}s"


def _esc(value: object) -> str:
    return html_lib.escape(str(value if value is not None else ""))


def _to_ist(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_IST)


def _phase_seconds(run_timing: dict | None) -> tuple[float | None, float | None]:
    """(scrape seconds, Resdex & outreach seconds) from run_timing["phases"]."""
    phases = (run_timing or {}).get("phases") or {}
    if not phases:
        return None, None
    scrape = phases.get("scrape_s")
    post = None
    if phases.get("outreach_s") is not None or phases.get("resdex_sweep_s") is not None:
        post = float(phases.get("outreach_s") or 0) + float(phases.get("resdex_sweep_s") or 0)
    return scrape, post


def _attachment_note(attachment_names: list[str] | None) -> str:
    """Footer sentence describing what is actually attached."""
    names = attachment_names or []
    kinds = []
    if any(n.lower().endswith(".xlsx") for n in names):
        kinds.append("Excel")
    if any(n.lower().endswith(".json") for n in names):
        kinds.append("JSON")
    if not kinds:
        return "No report file is attached to this email."
    return f"The detailed data is attached as {' and '.join(kinds)}."


# ── Plain-text fallback ─────────────────────────────────────────────────────

def _render_body(
    run_id: str,
    status: str,
    total_jobs: int,
    sources: list[str],
    error: str = "",
    insights: dict | None = None,
    run_timing: dict | None = None,
    attachment_names: list[str] | None = None,
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

    g = (insights or {}).get
    total = g("total", total_jobs) if insights else total_jobs
    lines = (
        "Hello,\n\n"
        f"The harvest run '{run_id}' has completed.\n\n"
        f"Status:     {status}\n"
        f"Sources:    {', '.join(sources) or '-'}\n"
        f"Total jobs: {total}\n"
    )
    # dupes = (g("total_rows", total) or 0) - total if insights else 0
    # if dupes > 0:
    #     lines += f"            ({dupes} duplicate listings across sources counted once)\n"
    # deferred = g("deferred_jobs", 0) if insights else 0
    # if deferred:
    #     lines += f"            (+{deferred} jobs from earlier stopped run(s) in the attachments)\n"
    lines += "\n"
    if run_timing:
        started = _to_ist(run_timing.get("started"))
        completed = _to_ist(run_timing.get("completed"))
        scrape_s, post_s = _phase_seconds(run_timing)
        lines += (
            "Run timing (IST)\n"
            f"- Start: {started.strftime('%d %b %Y, %H:%M:%S') if started else '-'}\n"
            f"- End:   {completed.strftime('%d %b %Y, %H:%M:%S') if completed else '-'}\n"
            f"- Duration: {_fmt_duration(run_timing.get('duration_s', 0))}\n"
        )
        if scrape_s is not None:
            lines += f"  - Scrape: {_fmt_duration(scrape_s)}\n"
        if post_s is not None:
            lines += f"  - Resdex & outreach: {_fmt_duration(post_s)}\n"
        lines += "\n"
    if insights:
        ps = g("per_source", {})
        lines += "Source breakdown\n"
        for src in ("LinkedIn", "Naukri", "Dice"):
            s = ps.get(src, {})
            if s.get("jobs"):
                lines += (
                    f"- {src}: {s['jobs']} jobs, {s.get('email', 0)} email, "
                    f"{s.get('phone', 0)} phone, {s.get('linkedin', 0)} LinkedIn\n"
                )
        # lines += f"\nContactable: {g('with_contact', 0)} of {total}\n"
        if g("apollo_credits_run") is not None:
            lines += f"\nApollo credits (this run): {g('apollo_credits_run')}\n"
        lines += "\n"
    if attachment_names:
        lines += "The extracted jobs are attached to this email.\n\n"
    lines += "Regards,\nSightspectrum Harvest Agent"
    return lines


# ── HTML building blocks (Outlook-safe) ──────────────────────────────────────

_FONT = "font-family:'Segoe UI',Arial,Helvetica,sans-serif;"
_MUTED = "#6b7280"
_INK = "#1a1d26"
_LINE = "#e5e7eb"
_TABLE = 'role="presentation" cellpadding="0" cellspacing="0" border="0"'


def _table(inner: str) -> str:
    """Full-width presentation table."""
    return (
        f'<table {_TABLE} width="100%" style="width:100%;border-collapse:collapse;">'
        f"{inner}</table>"
    )


def _cell(content: str, style: str = "", attrs: str = "") -> str:
    """A ``<td>`` with the font stack applied (Outlook doesn't inherit it reliably)."""
    return f'<td{(" " + attrs) if attrs else ""} style="{_FONT}{style}">{content}</td>'


def _bg_cell(content: str, color: str, style: str = "", attrs: str = "") -> str:
    """A ``<td>`` whose background is set both ways (bgcolor attr + inline CSS)."""
    attrs = f'bgcolor="{color}"' + (f" {attrs}" if attrs else "")
    return _cell(content, f"background-color:{color};{style}", attrs)


def _spacer_cell(height: int, color: str = "") -> str:
    style = f"height:{height}px;font-size:0;line-height:0;"
    if color:
        return (
            f'<td height="{height}" bgcolor="{color}" '
            f'style="{style}background-color:{color};">&nbsp;</td>'
        )
    return f'<td height="{height}" style="{style}">&nbsp;</td>'


def _row(content: str, padding: str = "24px 32px", style: str = "") -> str:
    """One full-width card row (padding lives on the td only)."""
    return "<tr>" + _cell(
        content, f"padding:{padding};font-size:14px;line-height:1.6;color:{_INK};{style}"
    ) + "</tr>"


def _divider(margin: int = 32) -> str:
    """A 1px rule inset ``margin`` px on each side, as a bgcolor cell."""
    inner = _table("<tr>" + _spacer_cell(1, _LINE) + "</tr>")
    return "<tr>" + _cell(inner, f"padding:0 {margin}px;") + "</tr>"


def _section_title(title: str) -> str:
    return (
        f'<div style="font-size:11px;font-weight:700;letter-spacing:0.1em;'
        f'text-transform:uppercase;color:{_MUTED};margin:0 0 16px;">{_esc(title)}</div>'
    )


def _bar(pct: float, color: str, track: str = _LINE, height: int = 8) -> str:
    """Progress bar as a nested 2-cell table: a filled cell ``pct``% wide and the
    track cell. Clamped to 0–100; a zero-width cell is omitted (Outlook would still
    render it with a minimum width)."""
    pct = max(0, min(100, int(round(pct or 0))))
    cells = ""
    if pct > 0:
        cells += (
            f'<td width="{pct}%" height="{height}" bgcolor="{color}" style="width:{pct}%;'
            f'height:{height}px;background-color:{color};font-size:0;line-height:0;">&nbsp;</td>'
        )
    if pct < 100:
        cells += (
            f'<td height="{height}" bgcolor="{track}" style="height:{height}px;'
            f'background-color:{track};font-size:0;line-height:0;">&nbsp;</td>'
        )
    return _table(f"<tr>{cells}</tr>")


def _kv_row(label: str, value: str, label_width: int = 170) -> str:
    """One label/value pair as a 2-cell row (value already escaped/HTML)."""
    return (
        "<tr>"
        + _cell(
            _esc(label),
            f"padding:0 0 10px;font-size:11px;font-weight:600;letter-spacing:0.03em;"
            f"text-transform:uppercase;color:{_MUTED};",
            f'width="{label_width}" valign="top"',
        )
        + _cell(value, f"padding:0 0 10px;font-size:15px;font-weight:700;color:{_INK};",
                'valign="top"')
        + "</tr>"
    )


_ICONS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "static" / "email-icons"
_SRC_SLUGS = {
    "LinkedIn": "linkedin", "Naukri": "naukri", "Dice": "dice",
    "LinkedIn Feed": "linkedin",
}
_SRC_COLORS = {
    "LinkedIn": "#0a66c2", "Naukri": "#2563eb", "Dice": "#ef4444",
    "LinkedIn Feed": "#0a66c2",
}
_SRC_ALT = {"LinkedIn": "in", "Naukri": "N", "Dice": "D", "LinkedIn Feed": "in"}
# Logos whose artwork is a circle — clipped round so no square corners show.
_SRC_ROUND = {"naukri"}


def _icon_file(slug: str) -> Path | None:
    # PNG first: Gmail and Outlook don't render SVG images.
    for ext in ("png", "jpg", "jpeg", "svg"):
        candidate = _ICONS_DIR / f"{slug}.{ext}"
        if candidate.is_file():
            return candidate
    return None


def _src_logo(name: str, size: int = 20) -> str:
    """The bare source logo (CID inline image, attached by
    ``collect_source_icon_attachments``). Falls back to a coloured letter tile
    only when the icon file is missing."""
    slug = _SRC_SLUGS.get(name)
    if slug and _icon_file(slug):
        # Shape (round / rounded) is baked into the PNG's alpha, not CSS — clients
        # clip border-radius inconsistently. vertical-align:middle keeps the image
        # centred where a client strips display:block and it falls back to inline.
        return (
            f'<img src="cid:src-{slug}" alt="{_esc(name)}" title="{_esc(name)}" '
            f'width="{size}" height="{size}" style="display:block;width:{size}px;'
            f'height:{size}px;border:0;outline:none;text-decoration:none;'
            f'vertical-align:middle;" />'
        )
    radius = "50%" if slug in _SRC_ROUND else f"{max(3, size // 5)}px"
    color = _SRC_COLORS.get(name, _MUTED)
    return (
        f'<table {_TABLE} width="{size}" style="border-collapse:collapse;"><tr>'
        + _bg_cell(
            _esc(_SRC_ALT.get(name, name[:1])), color,
            f"width:{size}px;height:{size}px;font-size:{max(9, size // 2)}px;font-weight:800;"
            f"color:#ffffff;text-align:center;line-height:{size}px;border-radius:{radius};",
            f'width="{size}" height="{size}" align="center" valign="middle"',
        )
        + "</tr></table>"
    )


def _src_badge(
    name: str, size: int = 15, font_size: int = 13, label: str | None = None,
) -> str:
    """Logo + source name, vertically centred in a 2-cell table pinned to the
    logo's height.

    No "exactly" line-height rule anywhere: Outlook (Word) seats the image on the
    text baseline and clips it to an exact line box (top cut off, logo too high),
    and puts an exact line's extra leading above the label (label too low)."""
    return (
        f'<table {_TABLE} height="{size}" style="border-collapse:collapse;height:{size}px;">'
        "<tr>"
        + _cell(_src_logo(name, size),
                f"width:{size}px;height:{size}px;font-size:1px;line-height:1px;"
                f"mso-line-height-rule:at-least;padding:0;",
                f'width="{size}" height="{size}" valign="middle"')
        + _cell(_esc(name if label is None else label),
                f"height:{size}px;line-height:{font_size + 3}px;mso-line-height-rule:at-least;"
                f"padding:0 0 0 7px;font-size:{font_size}px;font-weight:700;"
                f"color:{_INK};white-space:nowrap;",
                f'height="{size}" valign="middle"')
        + "</tr></table>"
    )


def _src_badges(
    names: list[str], size: int = 15, font_size: int = 13, gap: int = 12,
    show_names: bool = True,
) -> str:
    """Several sources side by side in one row (one cell per source)."""
    if not names:
        return ""
    cells = []
    for i, name in enumerate(names):
        content = _src_badge(name, size, font_size) if show_names else _src_logo(name, size)
        pad = f"padding:0 {gap}px 0 0;" if i < len(names) - 1 else ""
        # Same Outlook-safe line box as _src_badge's logo cell: line-height:0 makes
        # Word clip a bare logo's top.
        cells.append(_cell(
            content, f"{pad}font-size:1px;line-height:1px;mso-line-height-rule:at-least;",
            'valign="middle"',
        ))
    return f'<table {_TABLE} style="border-collapse:collapse;"><tr>{"".join(cells)}</tr></table>'


def collect_source_icon_attachments(html: str) -> list[dict]:
    """Scan rendered HTML for ``cid:src-*`` references and return an
    ``InlinedAttachments``-shaped list (Mailjet dict format) with the icon bytes
    base64-encoded. Supports PNG, SVG, and JPEG icon files. The actual content
    type is detected from the file's magic bytes — not the extension — so a PNG
    renamed to .svg still works correctly."""
    import base64 as b64
    import re

    cids = set(re.findall(r'cid:src-([\w-]+)', html))
    attachments: list[dict] = []
    for slug in sorted(cids):
        path = _icon_file(slug)
        if path is None:
            continue
        data = path.read_bytes()
        # Detect actual content type from magic bytes, not extension
        if data[:8].startswith(b'\x89PNG\r\n\x1a\n'):
            ctype, fname = "image/png", f"{slug}.png"
        elif data[:2] in (b'\xff\xd8',):
            ctype, fname = "image/jpeg", f"{slug}.jpg"
        elif b'<svg' in data[:512]:
            ctype, fname = "image/svg+xml", f"{slug}.svg"
        else:
            ctype, fname = "image/png", path.name
        attachments.append({
            "ContentType": ctype,
            "Filename": fname,
            "ContentID": f"src-{slug}",
            "Base64Content": b64.b64encode(data).decode("ascii"),
        })
    return attachments


def embed_source_icons_for_preview(html: str, attachments: list[dict] | None = None) -> str:
    """Browser-preview copy of a report: each ``cid:src-*`` becomes a data: URI built
    from the same inline attachments a real send carries. Preview only — Gmail
    blocks data: images, so never send this version."""
    import re

    if attachments is None:
        attachments = collect_source_icon_attachments(html)
    by_cid = {a["ContentID"]: a for a in attachments}

    def _sub(m: re.Match[str]) -> str:
        a = by_cid.get(m.group(1))
        return f"data:{a['ContentType']};base64,{a['Base64Content']}" if a else m.group(0)

    return re.sub(r"cid:(src-[\w-]+)", _sub, html)


def _split_pair(label: str, value: str, label_style: str, value_style: str) -> str:
    """Label left / value right, as a 2-cell table (replaces flex space-between)."""
    return _table(
        "<tr>"
        + _cell(label, label_style, 'align="left" valign="bottom"')
        + _cell(value, f"text-align:right;{value_style}", 'align="right" valign="bottom"')
        + "</tr>"
    )


def _doc(title: str, inner_rows: str, bar_color: str, bar_gradient: str) -> str:
    """Full HTML document: outer 100% table → centred 600px card with a solid 6px
    top bar (gradient as an optional background-image after the bgcolor)."""
    top_bar = (
        f'<tr><td height="6" bgcolor="{bar_color}" style="height:6px;'
        f'background-color:{bar_color};background-image:{bar_gradient};'
        f'font-size:0;line-height:0;">&nbsp;</td></tr>'
    )
    card = (
        f'<table {_TABLE} width="600" align="center" bgcolor="#ffffff" '
        f'style="width:600px;border-collapse:collapse;background-color:#ffffff;'
        f'border:1px solid {_LINE};">{top_bar}{inner_rows}</table>'
    )
    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="X-UA-Compatible" content="IE=edge">'
        f"<title>{_esc(title)}</title>"
        "<!--[if mso]><style>table,td,div,span,p{font-family:Arial,Helvetica,sans-serif"
        " !important;}</style><![endif]-->"
        "</head>"
        '<body style="margin:0;padding:0;background-color:#f3f4f6;" bgcolor="#f3f4f6">'
        f'<table {_TABLE} width="100%" bgcolor="#f3f4f6" '
        'style="width:100%;border-collapse:collapse;background-color:#f3f4f6;"><tr>'
        f'<td align="center" style="padding:24px 0;{_FONT}">'
        f'<!--[if mso]><table {_TABLE} width="600" align="center"><tr><td><![endif]-->'
        f"{card}"
        "<!--[if mso]></td></tr></table><![endif]-->"
        "</td></tr></table></body></html>"
    )


# ── HTML email template ──────────────────────────────────────────────────────
# Single combined template: top bar → header → hero metric → stat pills →
# run details → source breakdown table → Apollo credits → footer.

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
    attachment_names: list[str] | None = None,
) -> str:
    src_str = _esc(", ".join(sources) or "-")

    if status == "failed":
        return _render_failed_html(run_id, status, src_str, error)

    # ── Timing ───────────────────────────────────────────────────────────
    started = completed = duration = run_date = "-"
    scrape_s = post_s = None
    if run_timing:
        s = _to_ist(run_timing.get("started"))
        c = _to_ist(run_timing.get("completed"))
        if s:
            started = s.strftime("%H:%M:%S IST")
            run_date = s.strftime("%d %b %Y")
        if c:
            completed = c.strftime("%H:%M:%S IST")
        duration = _fmt_duration(run_timing.get("duration_s", 0))
        scrape_s, post_s = _phase_seconds(run_timing)

    # ── Insights ─────────────────────────────────────────────────────────
    g = (insights or {}).get
    ps = g("per_source", {})
    with_email = g("with_email", 0)
    with_phone = g("with_phone", 0)
    with_linkedin = g("with_linkedin", 0)
    apollo_credits_run = g("apollo_credits_run")  # this run's API enrichment credits
    # One denominator everywhere (hero, Total row, gauges): distinct postings.
    total = g("total", total_jobs) if insights else total_jobs

    status_color = {
        "success": "#10b981", "completed": "#10b981", "failed": "#ef4444",
        "stopped": "#f59e0b",
    }.get(status, _MUTED)

    # ── Header ───────────────────────────────────────────────────────────
    badge = (
        f'<table {_TABLE} align="right" style="border-collapse:collapse;"><tr>'
        + _bg_cell(
            _esc(status), status_color,
            "padding:3px 10px;font-size:11px;font-weight:700;letter-spacing:0.04em;"
            "text-transform:uppercase;color:#ffffff;",
        )
        + "</tr></table>"
    )
    header = (
        f'<div style="font-size:11px;font-weight:700;letter-spacing:0.1em;'
        f'text-transform:uppercase;color:{_MUTED};margin-bottom:8px;">Sightspectrum</div>'
        f'<div style="font-size:24px;font-weight:800;margin:0;">Harvest Run Report</div>'
        + _table(
            "<tr>"
            + _cell(
                f'<table {_TABLE} style="border-collapse:collapse;"><tr>'
                + _cell(f"{_esc(run_date)}" + (" &middot;" if sources else ""),
                        f"padding:0 10px 0 0;font-size:13px;color:{_MUTED};white-space:nowrap;",
                        'valign="middle"')
                + _cell(_src_badges(sources), "", 'valign="middle"')
                + "</tr></table>",
                "padding:4px 0 0;", 'valign="middle"')
            + _cell(badge, "padding:4px 0 0;", 'width="110" align="right" valign="middle"')
            + "</tr>"
        )
    )

    # ── Hero ─────────────────────────────────────────────────────────────
    hero = (
        f'<div style="font-size:64px;font-weight:900;line-height:1;color:#0ea5e9;">'
        f"{_esc(total)}</div>"
        f'<div style="font-size:13px;font-weight:600;letter-spacing:0.06em;'
        f'text-transform:uppercase;color:{_MUTED};margin-top:6px;">Jobs Harvested</div>'
    )

    # ── Stat pills (1-row table, spacer cells between) ───────────────────
    pills = [(duration, "Duration"), (len(sources), "Sources")]
    if apollo_credits_run is not None:
        pills.append((apollo_credits_run, "Apollo Credits"))
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
        + '<td width="8" style="width:8px;font-size:0;line-height:0;">&nbsp;</td>'.join(pill_cells)
        + "</tr></table>"
    )

    # ── Run details ──────────────────────────────────────────────────────
    detail_rows = (
        _kv_row("Run Date", _esc(run_date))
        + _kv_row("Sources", src_str)
        + _kv_row("Start Time", _esc(started))
        + _kv_row("End Time", _esc(completed))
        + _kv_row("Duration", _esc(duration))
    )
    if scrape_s is not None:
        detail_rows += _kv_row("Scrape phase", _esc(_fmt_duration(scrape_s)))
    if post_s is not None:
        detail_rows += _kv_row("Resdex & outreach", _esc(_fmt_duration(post_s)))
    details = _section_title("Run Details") + _table(detail_rows)

    # ── Source breakdown ─────────────────────────────────────────────────
    def _th(text: str, align: str) -> str:
        return _cell(
            text,
            f"padding:0 0 10px;font-size:10px;font-weight:700;letter-spacing:0.08em;"
            f"text-transform:uppercase;color:{_MUTED};text-align:{align};",
            f'align="{align}"' + (' width="200"' if align == "left" else ""),
        )

    def _src_td(content: str, align: str, last: bool = False) -> str:
        top = f"border-top:2px solid {_LINE};" if last else f"border-top:1px solid {_LINE};"
        weight = "800" if last else "700"
        color = f"color:{_MUTED};" if last and align == "left" else ""
        return _cell(
            content, f"padding:12px 0;{top}font-size:14px;font-weight:{weight};"
            f"text-align:{align};{color}", f'align="{align}" valign="middle"',
        )

    src_rows = "<tr>" + _th("Source", "left") + "".join(
        _th(h, "center") for h in ("Jobs", "Email", "Phone", "LinkedIn")
    ) + "</tr>"
    for name in ("LinkedIn", "Naukri", "Dice"):
        s = ps.get(name, {})
        if not s.get("jobs"):
            continue
        src_rows += (
            "<tr>" + _src_td(_src_badge(name, 24, 14), "left")
            + "".join(_src_td(str(int(s.get(k, 0) or 0)), "center")
                      for k in ("jobs", "email", "phone", "linkedin"))
            + "</tr>"
        )
    src_rows += (
        "<tr>" + _src_td("Total", "left", last=True)
        + "".join(_src_td(_esc(v), "center", last=True)
                  for v in (total, with_email, with_phone, with_linkedin))
        + "</tr>"
    )
    breakdown = _section_title("Source Breakdown") + _table(src_rows)

    # ── Apollo credits ───────────────────────────────────────────────────
    apollo_html = ""
    am = apollo_monthly or {}
    monthly_used = am.get("monthly_used", 0)
    monthly_cap = am.get("monthly_cap", 0)
    monthly_remaining = am.get("monthly_remaining")

    if monthly_used or (monthly_cap and monthly_cap > 0):
        monthly_rem_label = (
            f"{monthly_remaining} / {monthly_cap}"
            if monthly_remaining is not None and monthly_cap and monthly_cap > 0
            else "Unlimited"
        )
        monthly_pct = (
            round(monthly_used * 100 / monthly_cap) if monthly_cap and monthly_cap > 0 else 0
        )
        monthly_bar_color = "#f59e0b" if monthly_pct < 80 else "#ef4444"

        # Apollo's billing cycle (e.g. 25th → 25th) when known, else the calendar month.
        cycle_end = _to_ist(am.get("cycle_end"))
        period = "this billing cycle" if cycle_end else "this month"
        monthly_inner = _split_pair(
            "Cycle Remaining" if cycle_end else "Monthly Remaining", _esc(monthly_rem_label),
            f"font-size:11px;color:{_MUTED};font-weight:600;text-transform:uppercase;"
            "letter-spacing:0.04em;",
            "font-size:20px;font-weight:900;color:#059669;",
        )
        if monthly_cap and monthly_cap > 0:
            monthly_inner += (
                _table("<tr>" + _cell(_bar(monthly_pct, monthly_bar_color), "padding:10px 0 0;")
                       + "</tr>")
                + f'<div style="font-size:11px;color:{_MUTED};margin-top:4px;">'
                f"{_esc(monthly_used)} of {_esc(monthly_cap)} credits used {period} "
                f"({monthly_pct}%)"
                + (f" &middot; resets {_esc(cycle_end.strftime('%d %b %Y'))}" if cycle_end else "")
                + "</div>"
            )
        apollo_html = _divider(0) + _row(
            _section_title("Apollo Credits")
            + _table(
                "<tr>"
                + _cell(_table("<tr>" + _bg_cell(monthly_inner, "#f0fdf4", "padding:14px 16px;")
                               + "</tr>"), "padding:6px;")
                + "</tr>"
            )
        )

    footer = f"{_esc(_attachment_note(attachment_names))}<br>Sightspectrum Harvest Agent"

    rows = (
        _row(header, "28px 32px 0")
        + _row(hero, "32px 32px 24px", "text-align:center;")
        + _row(pills_html, "0 32px 24px", "text-align:center;")
        + _divider()
        + _row(details)
        + _divider()
        + _row(breakdown)
        + apollo_html
        + _divider(0)
        + _row(footer, "20px 32px", f"text-align:center;font-size:12px;color:{_MUTED};")
    )
    return _doc(f"Harvest Run Report — {run_id}", rows, "#0ea5e9",
                "linear-gradient(135deg,#0ea5e9,#8b5cf6)")


def _render_failed_html(run_id: str, status: str, src_str: str, error: str) -> str:
    header = (
        f'<div style="font-size:11px;font-weight:700;letter-spacing:0.1em;'
        f'text-transform:uppercase;color:{_MUTED};margin-bottom:8px;">Sightspectrum</div>'
        f'<div style="font-size:22px;font-weight:800;margin:0;">Harvest Run Failed</div>'
        f'<div style="font-size:13px;color:{_MUTED};margin-top:4px;">Run {_esc(run_id)}</div>'
    )
    error_box = _table("<tr>" + _bg_cell(
        f'<div style="font-size:13px;font-weight:700;color:#dc2626;margin-bottom:8px;">'
        f"Error details</div>"
        f'<div style="font-size:14px;color:#991b1b;">{_esc(error or "unknown")}</div>',
        "#fef2f2", "padding:20px 24px;border:1px solid #fecaca;",
    ) + "</tr>")
    body = (
        error_box
        + f'<div style="margin-top:16px;font-size:14px;color:{_MUTED};">'
        f"Status: {_esc(status)}<br>Sources: {src_str}<br>"
        "No report is attached — the run did not reach the export step.</div>"
    )
    rows = (
        _row(header, "28px 32px")
        + _row(body, "0 32px 24px")
        + _divider(0)
        + _row("Sightspectrum Harvest Agent", "20px 32px",
               f"text-align:center;font-size:12px;color:{_MUTED};")
    )
    return _doc(f"Harvest Run Failed — {run_id}", rows, "#ef4444",
                "linear-gradient(135deg,#ef4444,#f59e0b)")


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
    apollo_usage: dict | None = None,
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
    if job_dicts and notifications.report_format:
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

    # No attachments (report_format=None, no job records, or an Excel build that
    # yielded nothing): the summary email still goes out — its body carries the
    # run's numbers and states that no report file is attached. A failed run never
    # reached the export step, so it never has attachments.
    if not attachments and status != "failed":
        log.info(
            "harvest_report_no_attachments",
            report_format=notifications.report_format,
            job_records=len(job_dicts or []),
        )

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
        # Prefer Apollo's own counter (apollo_credit_usage.report_usage) over the local
        # reservation counters, which can't see every charge.
        if apollo_usage:
            apollo_daily = dict(apollo_daily or {})
            if apollo_usage.get("today") is not None:
                apollo_daily["global_used"] = apollo_usage["today"]
            apollo_monthly = {
                "monthly_used": apollo_usage["consumed"],
                "monthly_cap": apollo_usage["limit"],
                "monthly_remaining": apollo_usage["left_over"],
                "cycle_end": apollo_usage.get("cycle_end"),
            }

    attachment_names = [name for name, _ in attachments]
    body = _render_body(
        run_id, status, total_jobs, sources, error, insights, run_timing, attachment_names,
    )
    html_body = _render_body_html(
        run_id, status, total_jobs, sources, error, insights, run_timing,
        apollo_daily, apollo_monthly, attachment_names,
    )
    # From = the no-reply report address (OTP_FROM_EMAIL) under the configured
    # display name — not an outreach send, so no Reply-To / extra Reply-Tos.
    sender_identity = report_identity_from(email_sender.settings, notifications.email_cover_label)

    try:
        log.info(
            "harvest_report_email_sending", recipients=recipients, attachments=len(attachments),
        )
        icon_atts = collect_source_icon_attachments(html_body) if html_body else []
        await email_sender.send_email_with_attachments(
            recipients, subject, body, attachment_blobs=attachments, html_body=html_body,
            sender_identity=sender_identity, inline_attachments=icon_atts,
        )
        log.info(
            "harvest_report_email_sent",
            recipients=recipients,
            attachments=attachment_names,
        )
    except Exception:
        log.exception(
            "harvest_report_email_failed",
            recipients=recipients,
            attachments=len(attachments),
        )
