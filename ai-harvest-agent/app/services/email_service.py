"""Mailjet delivery for OTP, outreach, and harvest-report emails.

Sends over the Mailjet Send API v3.1 (POST https://api.mailjet.com/v3.1/send) with
HTTP Basic auth (public/private key), using the app-wide async ``httpx`` idiom (see
apollo_client.py / llm_service.py) — no SMTP, no ``asyncio.to_thread``, no new
dependency. AuthService depends on ``send_otp``; the outreach flow
(app/routes/outreach_routes.py) and the harvest report flow
(app/services/harvest_notification_service.py) depend on
``send_email_with_attachments`` — both share the same Mailjet transport/settings.

The public method signatures are unchanged from the previous SMTP implementation so
every call site (and tests/conftest.py::MockEmailSender) keeps working; only the
transport differs. ``send_email_with_attachments`` still returns a stable
``Message-ID`` string persisted as EmailOutreachORM.provider_message_id.
"""
from __future__ import annotations

import base64
import html as html_lib
import mimetypes
import re
from email.utils import formataddr, make_msgid, parseaddr
from pathlib import Path

import httpx
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


def _linkify_bare(escaped: str) -> str:
    """Turn bare http(s) URLs (deck link) and email addresses (reach-out line) in an
    already-HTML-escaped text segment into clickable links. Applied only to text
    OUTSIDE the job-title link, so an already-anchored URL is never wrapped twice."""
    if _BODY_URL_RE.search(escaped):
        escaped = _BODY_URL_RE.sub(
            lambda m: f'<a href="{m.group(0)}" style="color:#5f7fd0;">{m.group(0)}</a>',
            escaped,
        )
    if _BODY_EMAIL_RE.search(escaped):
        escaped = _BODY_EMAIL_RE.sub(
            lambda m: f'<a href="mailto:{m.group(0)}" style="color:#5f7fd0;">{m.group(0)}</a>',
            escaped,
        )
    return escaped


def _title_matcher(job_title: str) -> re.Pattern | None:
    """Case-insensitive, whitespace-tolerant matcher for the job title, so the
    posting link still attaches when the model reproduces the title with slightly
    different capitalization or spacing than the stored value (e.g. "Business
    Analyst" vs. "business  analyst"). Runs of whitespace in the title match any
    whitespace; every other character is matched literally. Returns None when
    there's no title to match."""
    normalized = " ".join((job_title or "").split())
    if not normalized:
        return None
    pattern = r"\s+".join(re.escape(tok) for tok in normalized.split(" "))
    return re.compile(pattern, re.IGNORECASE)


def _outreach_body_to_html(body: str, job_title: str = "", job_url: str = "") -> str:
    """Render the plain-text outreach body as HTML: preserve line breaks; turn any
    http(s) URL (the deck link) into a clickable link; turn any line containing an
    email address (the appended reach-out line) into a bold line whose address is a
    clickable mailto link — so the recipient can reply in one click; and, when
    `job_title`/`job_url` are given, render the first occurrence of the job title
    (the opening's role mention; matched case- and whitespace-insensitively) as a
    bold, blue link to the posting that opens in a new tab — the raw URL itself is
    never shown. All other text is HTML-escaped verbatim."""
    url = (job_url or "").strip()
    matcher = _title_matcher(job_title) if url else None
    title_linked = False
    out_lines: list[str] = []
    for line in (body or "").split("\n"):
        # Bold the whole line when it carries an email address (the reach-out line).
        bold = bool(_BODY_EMAIL_RE.search(line))
        match = matcher.search(line) if (matcher and not title_linked) else None
        if match:
            # Wrap only the first occurrence with a bold blue anchor that opens the
            # posting in a new tab; the URL stays hidden. Link the text exactly as the
            # model wrote it (match.group()), not the stored title, so casing/spacing
            # in the visible email is preserved.
            i, j = match.start(), match.end()
            anchor = (
                f'<a href="{html_lib.escape(url)}" target="_blank" '
                'rel="noopener noreferrer" '
                'style="color:#5f7fd0;font-weight:700;">'
                f"{html_lib.escape(line[i:j])}</a>"
            )
            rendered = (
                _linkify_bare(html_lib.escape(line[:i]))
                + anchor
                + _linkify_bare(html_lib.escape(line[j:]))
            )
            title_linked = True
        else:
            rendered = _linkify_bare(html_lib.escape(line))
        if bold:
            rendered = f"<strong>{rendered}</strong>"
        out_lines.append(rendered)
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


def _shared_identity_from(settings: Settings) -> dict:
    """The shared harvest-agent From used by OTP + outreach, as a Mailjet
    ``{"Email","Name"}`` object. When SMTP_FROM_EMAIL is a real address it's used
    as-is; when it's a bare display name (e.g. "JOB HARVEST AGENT") that name is
    shown and the mail is sent from the authenticated mailbox (SMTP_USERNAME).
    Mailjet's From.Email must be a real, account-validated address — never a bare
    display name (that's what caused the "'JOB' is an invalid email address" 400)."""
    configured = (settings.smtp_from_email or "").strip()
    username = (settings.smtp_username or "").strip()
    if "@" in configured:
        name, addr = parseaddr(configured)
        return {"Email": addr or username, "Name": name or "SS"}
    # Bare display name (or empty) → send from the authenticated mailbox and show
    # the display name as the sender name.
    return {"Email": username or configured, "Name": configured or "SS"}


def _parse_from(from_header: str, fallback_email: str) -> dict:
    """Turn a resolved RFC From header ("Name <addr>" or "addr") into a Mailjet
    ``{"Email","Name"}`` object (harvest report — configured sender identity).
    Falls back to ``fallback_email`` unless a real ``@`` address was parsed, so a
    bare display name never leaks into From.Email."""
    name, addr = parseaddr(from_header or "")
    email = addr if "@" in (addr or "") else fallback_email
    out = {"Email": email}
    if name:
        out["Name"] = name
    return out


def _build_attachments(
    attachment_paths: list[str],
    attachment_blobs: list[tuple[str, bytes]],
    log,
) -> list[dict]:
    """Base64-encode file-path and in-memory (filename, bytes) attachments into the
    Mailjet v3.1 ``Attachments`` shape."""
    out: list[dict] = []
    for raw_path in attachment_paths:
        path = Path(raw_path)
        log.debug("email_attachment_reading", path=str(path))
        data = path.read_bytes()
        maintype, subtype = _guess_attachment_type(path)
        out.append({
            "ContentType": f"{maintype}/{subtype}",
            "Filename": path.name,
            "Base64Content": base64.b64encode(data).decode("ascii"),
        })
    for filename, data in attachment_blobs:
        maintype, subtype = _guess_attachment_type(Path(filename))
        out.append({
            "ContentType": f"{maintype}/{subtype}",
            "Filename": filename,
            "Base64Content": base64.b64encode(data).decode("ascii"),
        })
    return out


class EmailSender:
    """Mailjet transport. AuthService only ever calls ``send_otp``; the outreach and
    harvest-report flows call ``send_email_with_attachments``."""

    _MAILJET_URL = "https://api.mailjet.com/v3.1/send"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send_otp(self, recipient: str, otp: str) -> None:
        settings = self._settings
        log = logger.bind(recipient=recipient)
        log.debug("otp_email_build_start")
        logo_bytes = _load_logo_bytes()

        # multipart/alternative: TextPart is the plain-text fallback, HTMLPart the
        # styled body. From is the shared "SS" identity (same as before).
        message: dict = {
            "From": _shared_identity_from(settings),
            "To": [{"Email": recipient}],
            "Subject": OTP_EMAIL_SUBJECT,
            "TextPart": render_otp_email(otp, settings.otp_expiry_seconds),
            "HTMLPart": render_otp_email_html(
                otp, settings.otp_expiry_seconds, has_logo=logo_bytes is not None
            ),
        }

        # Embed the logo as an inline (CID) image so it renders inline rather than
        # being blocked as a remote image — the HTML references cid:ss-logo.
        if logo_bytes is not None:
            message["InlinedAttachments"] = [{
                "ContentType": "image/jpeg",
                "Filename": "sight_spectrum_logo.jpg",
                "ContentID": LOGO_CID,
                "Base64Content": base64.b64encode(logo_bytes).decode("ascii"),
            }]

        await self._send_via_mailjet(message, log)
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
        job_title: str = "",
        job_url: str = "",
        custom_id: str | None = None,
    ) -> str | None:
        """Generic Mailjet send with attachments — same transport/credentials as
        send_otp. Attachments come as file paths and/or as in-memory
        (filename, bytes) blobs; the harvest report is generated from the DB
        in memory and attached as a blob.

        Returns a stable ``Message-ID`` string stamped on the message (so outreach
        sends can persist a provider identifier for follow-up threading); callers
        that don't need it can ignore the return. Raises on a Mailjet failure so the
        caller can record status="failed".

        Passing `from_email` marks this as an outreach send: the visible From
        becomes the shared "harvest agent" identity (the same "SS" sender the OTP
        email uses), and `from_email`/`reply_to` are set as the Reply-To so
        recruiter replies reach the salesperson. Callers that omit `from_email`
        (the harvest report) send under the configured sender identity resolved by
        _resolve_sender.

        HTML alternative part: pass `html_body` to supply a fully-formed HTML
        body (the harvest report renders its own) — `body` stays as the plain-text
        fallback. `as_html=True` instead derives the HTML from `body` (outreach:
        bold, clickable mailto reach-out line, and — when `job_title`/`job_url` are
        given — a bold blue job-title link that opens the posting in a new tab).
        With neither, the email is sent text-only.

        `custom_id` (outreach) is echoed by Mailjet in its delivery-event webhooks
        so a send row can be matched back to its events; passing it also turns on
        open + click tracking."""
        settings = self._settings
        paths = attachment_paths or []
        blobs = attachment_blobs or []
        log = logger.bind(recipients=recipients, subject=subject, attachments=len(paths) + len(blobs))
        log.debug("email_with_attachments_start")

        # Sender / Reply-To identity (mirrors the previous SMTP behavior):
        #  * Outreach (caller passes `from_email`) → shared "SS" identity; the
        #    sender's own address becomes the Reply-To so the recruiter's reply
        #    still reaches the salesperson.
        #  * Harvest report (no `from_email`) → configured sender identity
        #    (see _resolve_sender).
        if from_email:
            sender = _shared_identity_from(settings)
        else:
            from_header, envelope_from = _resolve_sender(settings)
            sender = _parse_from(from_header, envelope_from)

        # Stamp a Message-ID so outreach sends have a stable identifier to persist
        # (used for follow-up threading). Derive the domain from the configured
        # sender so it looks legitimate to receiving MTAs.
        sender_addr = parseaddr(settings.smtp_from_email or "")[1]
        msgid_domain = sender_addr.split("@")[-1] if "@" in sender_addr else None
        message_id = make_msgid(domain=msgid_domain) if msgid_domain else make_msgid()

        message: dict = {
            "From": sender,
            "To": [{"Email": r} for r in recipients],
            "Subject": subject,
            "TextPart": body,
            "Headers": {"Message-ID": message_id},
        }
        reply_addr = (reply_to or from_email or "").strip()
        if reply_addr:
            message["ReplyTo"] = {"Email": reply_addr}

        # HTML alternative: a pre-rendered report body wins; otherwise derive the
        # outreach HTML from `body`. With neither, the mail is text-only.
        if html_body is not None:
            message["HTMLPart"] = html_body
        elif as_html:
            message["HTMLPart"] = _outreach_body_to_html(body, job_title, job_url)

        attachments = _build_attachments(paths, blobs, log)
        if attachments:
            message["Attachments"] = attachments

        # Outreach only: correlate delivery events to the send row + track opens/clicks.
        if custom_id:
            message["CustomID"] = custom_id
            message["TrackOpens"] = "enabled"
            message["TrackClicks"] = "enabled"

        await self._send_via_mailjet(message, log)
        log.info("email_with_attachments_sent")
        return message_id

    async def _send_via_mailjet(self, message: dict, log=None) -> dict:
        """POST a single v3.1 message to the Mailjet Send API. Raises on a transport
        error or a non-success message status so callers can record the failure."""
        log = log or logger
        settings = self._settings
        if not settings.mailjet_api_key or not settings.mailjet_secret_key:
            raise RuntimeError(
                "Mailjet credentials are not configured "
                "(set MJ_APIKEY_PUBLIC / MJ_APIKEY_PRIVATE)."
            )
        payload = {"Messages": [message]}
        log.debug("mailjet_sending", to=message.get("To"))
        try:
            async with httpx.AsyncClient(timeout=settings.mailjet_timeout_seconds) as client:
                resp = await client.post(
                    self._MAILJET_URL,
                    json=payload,
                    auth=(settings.mailjet_api_key, settings.mailjet_secret_key),
                )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.error("mailjet_http_error", status=exc.response.status_code, body=exc.response.text[:500])
            raise
        except httpx.HTTPError as exc:
            log.error("mailjet_transport_error", error=str(exc))
            raise

        data = resp.json()
        result = (data.get("Messages") or [{}])[0]
        if str(result.get("Status", "")).lower() != "success":
            log.error("mailjet_message_rejected", result=result)
            raise RuntimeError(f"Mailjet rejected the message: {result}")
        log.debug("mailjet_send_ok")
        return result


def _guess_attachment_type(path: Path) -> tuple[str, str]:
    content_type, _ = mimetypes.guess_type(path.name)
    if content_type is None:
        return "application", "octet-stream"
    maintype, subtype = content_type.split("/", 1)
    return maintype, subtype
