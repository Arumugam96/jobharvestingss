"""SMTP delivery for OTP and harvest-report emails.

Uses the stdlib ``smtplib`` (run off the event loop via ``asyncio.to_thread``)
rather than a third-party async SMTP client, since the project has no such
dependency today and this keeps the login flow dependency-free. AuthService
depends on ``send_otp``; the harvest report flow (see
app/services/harvest_notification_service.py) depends on
``send_email_with_attachments`` — both share the same SMTP transport/settings.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import mimetypes
import re
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path

import structlog

from app.config import Settings

logger = structlog.get_logger(__name__)

OTP_EMAIL_SUBJECT = "Your Sightspectrum Login OTP"


def _resolve_sender(settings: Settings) -> tuple[str, str]:
    """Resolve the visible From header and the SMTP envelope sender for the
    harvest report from ``SMTP_FROM_EMAIL`` / ``SMTP_USERNAME``.

    Returns ``(from_header, envelope_addr)``.

    * ``from_header``  — what the recipient sees. If ``SMTP_FROM_EMAIL`` is a real
      address it's used as-is; if it's a bare display name (e.g.
      ``"JOB HARVEST AGENT"``) it becomes the display name paired with the
      authenticated mailbox — so the inbox shows that name instead of the Gmail
      account owner (which is why the harvest mail used to read "Hariprasath").
    * ``envelope_addr`` — the SMTP ``MAIL FROM``. Always a real address (never a
      bare display name, which providers reject): the configured address when it
      has one, else the authenticated mailbox.
    """
    username = (settings.smtp_username or "").strip()
    configured = (settings.smtp_from_email or "").strip()

    if "@" in configured:
        name, addr = parseaddr(configured)
        from_header = formataddr((name, addr)) if name else addr
        return from_header, (addr or username)

    if configured:
        # Display-name-only SMTP_FROM_EMAIL — show it as the sender name and send
        # from the authenticated mailbox.
        from_header = formataddr((configured, username)) if username else configured
        return from_header, username

    return username, username

# Matches an email address inside the plain-text outreach body so it can be
# rendered as a bold, clickable mailto link in the HTML part (the reach-out line).
_BODY_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Matches an http(s) URL (the hosted company-overview deck link) so it can be
# rendered as a clickable link in the HTML part.
_BODY_URL_RE = re.compile(r'https?://[^\s<>"]+')


def _outreach_body_to_html(body: str) -> str:
    """Render the plain-text outreach body as HTML: preserve line breaks, turn any
    line containing an email address (the appended reach-out line) into a bold line
    whose address is a clickable mailto link — so the recipient can reply to the
    sender in one click — and turn any http(s) URL (the deck link) into a clickable
    link. All other text is HTML-escaped verbatim."""
    out_lines: list[str] = []
    for line in (body or "").split("\n"):
        escaped = html_lib.escape(line)
        if _BODY_URL_RE.search(line):
            escaped = _BODY_URL_RE.sub(
                lambda m: f'<a href="{m.group(0)}" style="color:#5f7fd0;">{m.group(0)}</a>',
                escaped,
            )
        if _BODY_EMAIL_RE.search(line):
            escaped = _BODY_EMAIL_RE.sub(
                lambda m: f'<a href="mailto:{m.group(0)}" style="color:#5f7fd0;">{m.group(0)}</a>',
                escaped,
            )
            escaped = f"<strong>{escaped}</strong>"
        out_lines.append(escaped)
    inner = "<br>\n".join(out_lines)
    return (
        '<!DOCTYPE html><html><body style="margin:0;padding:0;">'
        "<div style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
        'Roboto,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.6;color:#222222;">'
        f"{inner}"
        "</div></body></html>"
    )

# Inline logo shipped with the backend. Embedded into the HTML email as a CID
# attachment (see EmailSender.send_otp) so it renders without being blocked as
# a remote image. LOGO_CID is the Content-ID the HTML references via cid:.
LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "sight_spectrum_logo.jpg"
LOGO_CID = "ss-logo"


def render_otp_email(otp: str, expiry_seconds: int) -> str:
    """Plain-text OTP email body — the fallback part for clients that don't
    render HTML. Edit this to restyle the plain-text email."""
    minutes = max(1, expiry_seconds // 60)
    return (
        "Hello,\n\n"
        "Your one-time password for signing in is:\n\n"
        f"{otp}\n\n"
        f"This OTP expires in {minutes} minute(s).\n\n"
        "If you did not request this OTP, please ignore this email.\n\n"
        "Regards,\n"
        "SS Harvesting Agent"
    )


def render_otp_email_html(otp: str, expiry_seconds: int, *, has_logo: bool) -> str:
    """HTML OTP email body.

    Table-based, inline-styled layout for broad email-client compatibility
    (Gmail, Outlook, Apple Mail). Colours are pulled from the Sightspectrum
    logo (blue -> violet -> magenta -> teal). Gradients degrade to a solid
    dark fill in clients that don't support them (e.g. Outlook/Word engine).

    ``has_logo`` toggles the header image; when False (logo file missing) the
    header falls back to the wordmark alone rather than a broken image.
    """
    minutes = max(1, expiry_seconds // 60)

    logo_cell = (
        f'<img src="cid:{LOGO_CID}" width="40" height="40" alt="Sightspectrum" '
        'style="display:block;border:0;border-radius:9px;background:#ffffff;padding:4px;" />'
        if has_logo
        else ""
    )

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="color-scheme" content="light only" />
<title>{OTP_EMAIL_SUBJECT}</title>
</head>
<body style="margin:0;padding:0;background:#ececef;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">Your verification code is {otp}. It expires in {minutes} minute(s).</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#ececef;">
<tr>
<td align="center" style="padding:32px 16px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:100%;background:#ffffff;border-radius:14px;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">

<!-- header -->
<tr>
<td style="background:#191634;background:linear-gradient(135deg,#191634 0%,#2a2150 60%,#3f2d6b 100%);padding:28px 40px;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0">
<tr>
<td style="padding-right:13px;">{logo_cell}</td>
<td style="font-size:18px;font-weight:700;color:#ffffff;letter-spacing:-0.01em;">Sight<span style="color:#cdbdf0;font-weight:500;">spectrum</span></td>
</tr>
</table>
</td>
</tr>

<!-- spectrum rule -->
<tr><td style="height:4px;line-height:4px;font-size:0;background:#8a3fb0;background:linear-gradient(90deg,#5f7fd0,#7b53c9,#a83fa6,#2b8fc0);">&nbsp;</td></tr>

<!-- body -->
<tr>
<td style="padding:38px 40px 8px;">
<p style="margin:0 0 10px;font-size:11px;letter-spacing:0.16em;text-transform:uppercase;color:#8a3fb0;font-weight:700;">Secure sign-in</p>
<h1 style="margin:0 0 10px;font-size:22px;line-height:1.25;font-weight:700;color:#191634;">Here's your one-time password</h1>
<p style="margin:0 0 26px;font-size:15px;line-height:1.6;color:#74738a;">Use the code below to finish signing in to your Sightspectrum account. For your security, don't share it with anyone.</p>

<!-- code card -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f4f1fb;border:1px solid #e0d7f3;border-radius:14px;">
<tr>
<td align="center" style="padding:24px;">
<p style="margin:0 0 14px;font-size:11px;letter-spacing:0.18em;text-transform:uppercase;color:#74738a;font-weight:600;">Your verification code</p>
<p style="margin:0;font-family:'SF Mono',ui-monospace,Consolas,'Liberation Mono',monospace;font-size:40px;font-weight:700;letter-spacing:0.24em;color:#191634;line-height:1;">{otp}</p>
<p style="margin:16px 0 0;font-size:12.5px;color:#8a3fb0;font-weight:600;">Double-click the code to select &amp; copy it</p>
</td>
</tr>
</table>

<!-- expiry -->
<p style="margin:24px 0 4px;text-align:center;font-size:13px;color:#b4531a;font-weight:500;">&#9200; This code expires in {minutes} minute(s)</p>

<!-- note -->
<p style="margin:26px 0 4px;padding-top:22px;border-top:1px solid #ebe9f2;font-size:12.5px;line-height:1.6;color:#74738a;">If you didn't request this code, you can safely ignore this email &mdash; someone may have entered your address by mistake. No changes will be made to your account.</p>
</td>
</tr>

<!-- footer -->
<tr>
<td style="padding:22px 40px 30px;text-align:center;font-size:11.5px;line-height:1.6;color:#9a99ab;">
<strong style="color:#74738a;">SS Harvesting Agent</strong><br />
This is an automated message from Sightspectrum. Please don't reply.
</td>
</tr>

</table>
</td>
</tr>
</table>
</body>
</html>
"""


def _load_logo_bytes() -> bytes | None:
    """Read the inline logo, returning None (and logging) if it's missing so
    the email still sends with the wordmark-only header."""
    try:
        return LOGO_PATH.read_bytes()
    except OSError:
        logger.warning("otp_email_logo_missing", path=str(LOGO_PATH))
        return None


class EmailSender:
    """SMTP abstraction. AuthService only ever calls ``send_otp``."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send_otp(self, recipient: str, otp: str) -> None:
        settings = self._settings
        log = logger.bind(recipient=recipient)
        log.debug("otp_email_build_start")
        logo_bytes = _load_logo_bytes()

        message = EmailMessage()
        message["Subject"] = OTP_EMAIL_SUBJECT
        message["From"] = f"SS {settings.smtp_from_email}"
        message["To"] = recipient

        # multipart/alternative: plain-text fallback first, then the HTML body.
        message.set_content(render_otp_email(otp, settings.otp_expiry_seconds))
        message.add_alternative(
            render_otp_email_html(
                otp, settings.otp_expiry_seconds, has_logo=logo_bytes is not None
            ),
            subtype="html",
        )

        # Embed the logo as an inline (CID) image related to the HTML part, so
        # it renders inline rather than being blocked as a remote image.
        if logo_bytes is not None:
            html_part = message.get_payload()[-1]
            html_part.add_related(
                logo_bytes,
                maintype="image",
                subtype="jpeg",
                cid=f"<{LOGO_CID}>",
                filename="sight_spectrum_logo.jpg",
            )

        await asyncio.to_thread(self._send_sync, message, log)
        log.info("otp_email_sent")

    async def send_email_with_attachments(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        attachment_paths: list[str] | None = None,
        attachment_blobs: list[tuple[str, bytes]] | None = None,
        from_email: str | None = None,
        reply_to: str | None = None,
        as_html: bool = False,
        html_body: str | None = None,
    ) -> None:
        """Generic SMTP send with attachments — same transport/credentials as
        send_otp. Attachments come as file paths and/or as in-memory
        (filename, bytes) blobs; the harvest report is generated from the DB
        in memory and attached as a blob.

        Passing `from_email` marks this as an outreach send: the visible From
        becomes the shared "harvest agent" identity ("SS <SMTP_FROM_EMAIL>", the
        same sender the OTP email uses), and `from_email`/`reply_to` are set as
        the Reply-To so recruiter replies reach the salesperson. Callers that omit
        `from_email` (the harvest report) send under the configured sender
        identity resolved by _resolve_sender.

        HTML alternative part: pass `html_body` to supply a fully-formed HTML
        body (the harvest report renders its own) — `body` stays as the plain-text
        fallback. `as_html=True` instead derives the HTML from `body` (outreach:
        bold, clickable mailto reach-out line). With neither, the email is sent
        text-only."""
        paths = attachment_paths or []
        blobs = attachment_blobs or []
        log = logger.bind(recipients=recipients, subject=subject, attachments=len(paths) + len(blobs))
        log.debug("email_with_attachments_start")
        await asyncio.to_thread(
            self._send_with_attachments_sync,
            recipients, subject, body, paths, blobs, from_email, reply_to, as_html, html_body, log,
        )
        log.info("email_with_attachments_sent")

    def _send_with_attachments_sync(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        attachment_paths: list[str],
        attachment_blobs: list[tuple[str, bytes]],
        from_email: str | None,
        reply_to: str | None,
        as_html: bool,
        html_body: str | None,
        log,
    ) -> None:
        # Sender identity:
        #  * Outreach (caller passes `from_email`) sends under the shared
        #    "harvest agent" identity exactly like the OTP email — From is
        #    "SS <SMTP_FROM_EMAIL>" and the envelope is left for the provider to
        #    derive. The sender's own address becomes the Reply-To below, so the
        #    recruiter's reply still reaches the salesperson.
        #  * Harvest report (no `from_email`) uses the configured sender identity
        #    (see _resolve_sender — this is the fix for the old "Hariprasath").
        if from_email:
            from_header, envelope_from = f"SS {self._settings.smtp_from_email}", None
        else:
            from_header, envelope_from = _resolve_sender(self._settings)
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = from_header
        if reply_to or from_email:
            message["Reply-To"] = reply_to or from_email
        message["To"] = ", ".join(recipients)
        message.set_content(body)
        # HTML alternative (added before attachments so the MIME tree is
        # multipart/mixed[ multipart/alternative[text, html], attachments… ]).
        # A caller-supplied html_body (the harvest report) wins; otherwise
        # as_html derives it from the plain-text body (outreach).
        html_part = html_body if html_body else (_outreach_body_to_html(body) if as_html else None)
        if html_part:
            message.add_alternative(html_part, subtype="html")

        for raw_path in attachment_paths:
            path = Path(raw_path)
            log.debug("email_attachment_reading", path=str(path))
            data = path.read_bytes()
            maintype, subtype = _guess_attachment_type(path)
            message.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)
            log.debug(
                "email_attachment_attached",
                path=str(path),
                bytes=len(data),
                content_type=f"{maintype}/{subtype}",
            )

        for filename, data in attachment_blobs:
            maintype, subtype = _guess_attachment_type(Path(filename))
            message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
            log.debug(
                "email_attachment_attached",
                filename=filename,
                bytes=len(data),
                content_type=f"{maintype}/{subtype}",
            )

        # envelope_from is a real address for the harvest report, or None for
        # outreach (provider derives it from the From, exactly like the OTP send).
        self._send_sync(message, log, envelope_from)

    def _send_sync(self, message: EmailMessage, log=None, from_addr: str | None = None) -> None:
        log = log or logger
        settings = self._settings
        log.debug(
            "smtp_connecting",
            host=settings.smtp_host,
            port=settings.smtp_port,
            use_tls=settings.smtp_use_tls,
        )
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds) as server:
                log.debug("smtp_connected")
                if settings.smtp_use_tls:
                    server.starttls()
                    log.debug("smtp_starttls_ok")
                if settings.smtp_username:
                    log.debug("smtp_login_attempt", username=settings.smtp_username)
                    server.login(settings.smtp_username, settings.smtp_password)
                    log.debug("smtp_login_ok")
                log.debug("smtp_sending_message", to=message["To"])
                if from_addr:
                    server.send_message(message, from_addr=from_addr)
                else:
                    server.send_message(message)
                log.debug("smtp_send_message_ok")
        except Exception:
            log.exception(
                "smtp_send_failed",
                host=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_username,
            )
            raise


def _guess_attachment_type(path: Path) -> tuple[str, str]:
    content_type, _ = mimetypes.guess_type(path.name)
    if content_type is None:
        return "application", "octet-stream"
    maintype, subtype = content_type.split("/", 1)
    return maintype, subtype
