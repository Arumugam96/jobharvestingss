"""Email delivery for OTP, outreach, and harvest-report emails.

Two interchangeable transports, selected by ``Settings.email_provider``:
  * ``"mailjet"`` (default) — the Mailjet Send API v3.1
    (POST https://api.mailjet.com/v3.1/send) with HTTP Basic auth, using the app-wide
    async ``httpx`` idiom (see apollo_client.py / llm_service.py).
  * ``"smtp"`` — a plain SMTP relay (stdlib ``smtplib`` on a worker thread), which is how
    mail is routed through Brevo (smtp-relay.brevo.com). Brevo tracks opens/clicks itself
    and echoes our row id back on its webhooks via the ``X-Mailin-custom`` header, which
    the SMTP path sets from ``CustomID``.

Both build the SAME Mailjet-shaped message dict; ``_dispatch`` picks the transport and
``_send_via_smtp`` translates that dict into a stdlib ``EmailMessage``. AuthService depends
on ``send_otp``; the outreach flow (app/routes/outreach_routes.py) and the harvest report
flow (app/services/harvest_notification_service.py) depend on ``send_email_with_attachments``.

The public method signatures are unchanged, so every call site (and
tests/conftest.py::MockEmailSender) keeps working; only the transport differs.
``send_email_with_attachments`` still returns a stable ``Message-ID``/provider reference
string persisted as EmailOutreachORM.provider_message_id.
"""
from __future__ import annotations

import asyncio
import base64
import html as html_lib
import mimetypes
import re
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from pathlib import Path

import httpx
import structlog

from app.config import Settings
from app.prompts.outreach_prompts import COMPANY_NAME, resolve_identity

logger = structlog.get_logger(__name__)

OTP_EMAIL_SUBJECT = "Your Sightspectrum Login OTP"

# Transient httpx transport failures worth retrying — network blips and broken/reset
# TLS handshakes to api.mailjet.com (the ConnectError(BrokenResourceError()) that was
# turning ~2 of every 5 auto-outreach sends into permanent "failed" rows), plus
# read/write/pool timeouts. Distinct from httpx.HTTPStatusError, which we retry only
# for the statuses in _RETRYABLE_STATUS.
_RETRYABLE_TRANSPORT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)
# Mailjet/edge HTTP statuses that represent a transient condition (rate-limit or a
# temporary server error) rather than a permanent rejection — safe to retry.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Transient SMTP failures worth retrying on the ``email_provider="smtp"`` path — a dropped
# relay connection, a connect failure, or a socket timeout. Authentication/recipient
# errors (SMTPAuthenticationError, SMTPRecipientsRefused, …) are permanent and NOT retried.
_RETRYABLE_SMTP_ERRORS = (
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPConnectError,
    ConnectionError,
    TimeoutError,
    OSError,
)

AUTOMATION_CONTACT_BLOCK = (
    "For any queries, please reach out to us:\n"
    "Shankar - +91 8056081469\n"
    "Sanjeetha - +91 9949099528\n"
)

_SIGNOFF_LEAD = "regards,"
_WEBSITE_LEAD = "more about us"


def _insert_above_signoff(body: str, block: str, lead: str = _SIGNOFF_LEAD) -> str:
    """Insert `block` (its own blank-line-separated paragraph) immediately ABOVE the
    paragraph that starts with `lead` (default: the trailing "Regards," sign-off), so
    that anchor paragraph — and everything after it — stays last in the email. Falls
    back to appending at the end only if no matching paragraph can be found."""
    text = (body or "").rstrip()
    extra = (block or "").strip()
    if not extra:
        return text
    paras = text.split("\n\n")
    for i in range(len(paras) - 1, -1, -1):
        if paras[i].lstrip().lower().startswith(lead):
            paras.insert(i, extra)
            return "\n\n".join(paras)
    return f"{text}\n\n{extra}"


def apply_automation_contact_block(body: str) -> str:
    """Insert AUTOMATION_CONTACT_BLOCK immediately above the closing website/deck line
    ("More about us: …") so the order reads: contact block → website link → sign-off.

    Shared by the outgoing-message render (send_email_with_attachments with
    is_automation=True) AND historically by the auto-outreach send-log write. The
    live auto-outreach flow now threads the block through append_closing instead, so
    this is a defensive fallback that still keeps the signature last if used."""
    return _insert_above_signoff(body, AUTOMATION_CONTACT_BLOCK, lead=_WEBSITE_LEAD)


def _resolve_sender(settings: Settings) -> tuple[str, str]:
    """Resolve the visible From header and the SMTP envelope sender for the
    harvest report.

    Returns ``(from_header, envelope_addr)``.

    * ``from_header``  — what the recipient sees.
    * ``envelope_addr`` — the SMTP ``MAIL FROM``. Always a real address (never a
      bare display name, which providers reject).

    Preferred: the explicit ``SMTP_SENDER_MAIL`` (+ ``SMTP_ENVELOPE_NAME`` display name) —
    a real, provider-verified sender, required for Brevo (the SMTP login is not a sendable
    From). When ``SMTP_SENDER_MAIL`` is unset, falls back to the legacy behavior: a real
    ``SMTP_FROM_EMAIL`` as-is, or a bare display name paired with the authenticated mailbox
    (``SMTP_USERNAME``) — so the inbox shows that name instead of the account owner.
    """
    sender_mail = (settings.smtp_sender_mail or "").strip()
    if sender_mail:
        envelope_name = (settings.smtp_envelope_name or "").strip()
        from_header = formataddr((envelope_name, sender_mail)) if envelope_name else sender_mail
        return from_header, sender_mail

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


_BODY_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_BODY_URL_RE = re.compile(r'https?://[^\s<>"]+')
_BODY_PHONE_RE = re.compile(r"\+\d[\d\s\-]{7,}\d")
_PHONE_SEP_RE = re.compile(r"[\s\-]")


def _phone_anchor(m: re.Match) -> str:
    """Render a matched phone number as a bold, highlighted ``tel:`` link. The href
    strips spaces/dashes (``+91 8056081469`` → ``tel:+918056081469``) so tapping it
    on a phone dials directly; the visible text keeps the readable spacing."""
    num = m.group(0)
    href = "tel:" + _PHONE_SEP_RE.sub("", num)
    return f'<a href="{href}" style="color:#5f7fd0;font-weight:700;">{num}</a>'


def _linkify_bare(escaped: str) -> str:
    """Turn bare http(s) URLs (deck link), email addresses (reach-out line), and
    phone numbers (desk contact block) in an already-HTML-escaped text segment into
    clickable links. Applied only to text OUTSIDE the job-title link, so an already-
    anchored URL is never wrapped twice. Phone numbers become bold, highlighted
    ``tel:`` links so tapping them on a phone opens the dialer."""
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
    if _BODY_PHONE_RE.search(escaped):
        escaped = _BODY_PHONE_RE.sub(_phone_anchor, escaped)
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


# Signature-card colours, pulled from the OTP email palette (navy → violet → blue).
_SIG_NAME_COLOR = "#201a4a"
_SIG_MUTED = "#5c5a70"
_SIG_ICON = "#7b53c9"
_SIG_LINK = "#5f7fd0"
_SIG_TEXT = "#3f3d55"
_SIG_HAIRLINE = "#c7bfe0"


def _website_display(url: str) -> str:
    """Human-readable form of a website URL for the signature — drops the scheme and any
    trailing slash: 'https://www.sightspectrum.com/' -> 'www.sightspectrum.com'."""
    return re.sub(r"^https?://", "", (url or "").strip()).rstrip("/")


def render_signature_html(
    name: str, title: str, phone: str, email: str, website: str, *, has_logo: bool
) -> str:
    """Business-card email signature rendered below the 'Regards,' 
    line in the HTML part:
    """
    esc = html_lib.escape
    logo_cell = (
        f'<td valign="top" style="padding:2px 16px 0 0;">'
        f'<img src="cid:{LOGO_CID}" width="84" height="84" alt="{esc(COMPANY_NAME)}" '
        'style="display:block;border:0;border-radius:10px;background:#ffffff;" />'
        "</td>"
        if has_logo else ""
    )

    role_line = esc(title)
    if COMPANY_NAME:
        role_line += f' <span style="color:{_SIG_HAIRLINE};">|</span> {esc(COMPANY_NAME)}'

    # Phone + email share one line (dot separator); website goes on its own line below.
    inline_bits: list[str] = []
    if phone:
        tel = "tel:" + _PHONE_SEP_RE.sub("", phone)
        inline_bits.append(
            f'<span style="color:{_SIG_ICON};">&#9742;</span>&nbsp;'
            f'<a href="{tel}" style="color:{_SIG_TEXT};text-decoration:none;">{esc(phone)}</a>'
        )
    if email:
        inline_bits.append(
            f'<span style="color:{_SIG_ICON};">&#9993;</span>&nbsp;'
            f'<a href="mailto:{esc(email)}" style="color:{_SIG_LINK};text-decoration:none;">{esc(email)}</a>'
        )
    contacts: list[str] = []
    if inline_bits:
        sep = f'<span style="color:{_SIG_HAIRLINE};">&nbsp;&nbsp;&bull;&nbsp;&nbsp;</span>'
        contacts.append(sep.join(inline_bits))
    if website:
        href = website if re.match(r"^https?://", website) else f"https://{website}"
        contacts.append(
            f'<span style="color:{_SIG_ICON};">&#127760;</span>&nbsp;'
            f'<a href="{esc(href)}" style="color:{_SIG_LINK};text-decoration:none;">'
            f"{esc(_website_display(website))}</a>"
        )
    contacts_html = "<br>".join(contacts)

    font = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif")
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="margin-top:8px;border-collapse:collapse;">'
        f"<tr>{logo_cell}"
        f'<td valign="middle" style="font-family:{font};">'
        f'<div style="font-size:19px;font-weight:800;color:{_SIG_NAME_COLOR};line-height:1.15;">{esc(name)}</div>'
        f'<div style="font-size:13px;color:{_SIG_MUTED};margin:3px 0 9px;">{role_line}</div>'
        f'<div style="font-size:13px;color:{_SIG_TEXT};line-height:1.7;">{contacts_html}</div>'
        "</td></tr></table>"
    )


def _outreach_body_to_html(
    body: str,
    job_title: str = "",
    job_url: str = "",
    show_unsub: bool = False,
    logo_available: bool = False,
) -> str:
    """Render the plain-text outreach body as HTML: preserve line breaks; turn any
    http(s) URL (the deck link) into a clickable link; turn any line containing an
    email address into a bold line whose address is a clickable mailto link; and, when
    `job_title`/`job_url` are given, render the first occurrence of the job title (the
    intro's role mention; matched case- and whitespace-insensitively) as a bold, blue
    link to the posting that opens in a new tab — the raw URL itself is never shown. All
    other text is HTML-escaped verbatim.

    The trailing sign-off ("Regards, / name / title / email") is NOT rendered as plain
    lines — it is replaced by a formatted business-card signature (render_signature_html):
    the "Regards," line is kept, then the signature card is built from the sender identity
    (resolve_identity, keyed by the email in the sign-off) plus the website (deck) link
    parsed from the body. `logo_available` toggles the logo cell so the card only points at
    the inline logo when it's actually attached to the message.

    When `show_unsub` is True, a subtle plain, link-free unsubscribe message is placed
    ABOVE the sign-off so the signature stays last (Mailjet/Brevo own the opt-out now)."""
    url = (job_url or "").strip()
    matcher = _title_matcher(job_title) if url else None
    title_linked = False

    all_lines = (body or "").split("\n")
    # Split off the sign-off ("Regards, …"): everything from that line down is replaced by
    # the formatted signature card. The lines above keep the normal per-line rendering.
    signoff_start = next(
        (i for i, ln in enumerate(all_lines) if ln.strip().lower().startswith(_SIGNOFF_LEAD)),
        None,
    )
    body_lines = all_lines if signoff_start is None else all_lines[:signoff_start]
    signoff_lines = [] if signoff_start is None else all_lines[signoff_start:]

    out_lines: list[str] = []
    website_url = ""  # first URL in the body = the "More about us" deck link (for the signature)
    for line in body_lines:
        if not website_url:
            um = _BODY_URL_RE.search(line)
            if um:
                website_url = um.group(0)
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

    if show_unsub:
        # Plain, link-free unsubscribe message — placed ABOVE the sign-off so the signature
        # stays the last thing in the email.
        out_lines.append(
            '<div style="margin-top:18px;font-size:12px;color:#94A3B8;">'
            'Not interested? Reply to this email to unsubscribe.'
            "</div>"
        )

    # Sign-off → "Regards," text line, then the formatted signature card in place of the
    # plain name/title/email lines. Identity (full name, role title, phone) is resolved from
    # the sender email in the sign-off block, so the card matches the intro and the plain-text
    # sign-off; the website is the deck link parsed from the body above.
    if signoff_lines:
        sender_email = ""
        for ln in signoff_lines:
            em = _BODY_EMAIL_RE.search(ln)
            if em:
                sender_email = em.group(0)
                break
        identity = resolve_identity(sender_email)
        out_lines.append(html_lib.escape(signoff_lines[0].strip()))  # "Regards,"
        out_lines.append(render_signature_html(
            name=identity["full"] or identity["first"] or COMPANY_NAME,
            title=identity["title"],
            phone=identity["phone"],
            email=sender_email,
            website=website_url,
            has_logo=logo_available,
        ))

    inner = "<br>\n".join(out_lines)
    return (
        '<!DOCTYPE html><html><body style="margin:0;padding:0;">'
        "<div style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
        'Roboto,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.6;color:#222222;">'
        f"{inner}"
        "</div></body></html>"
    )


def render_outreach_email_html(
    body: str,
    job_title: str = "",
    job_url: str = "",
    *,
    show_unsub: bool = True,
    logo_available: bool | None = None,
) -> str:
    """The exact HTML part an outreach send builds from its plain-text body — the
    single render source shared by send_email_with_attachments (as_html) and the
    send-log writers, which persist it as EmailOutreachORM.body_html so the log
    records the message as actually delivered (signature card included).

    The signature's logo is an inline CID image (`cid:ss-logo`), attached with no
    filename so mail clients render it in the card without listing it as an
    attachment. `logo_available` defaults to whether the logo file exists (the same
    condition under which the send attaches it); the send path passes the actual
    attach state so the HTML never points at a missing image. The stored body_html
    keeps the cid: reference — the image bytes are NOT persisted per row; the UI
    swaps the cid for its bundled copy of the logo when displaying the log."""
    if logo_available is None:
        logo_available = LOGO_PATH.is_file()
    return _outreach_body_to_html(
        body, job_title, job_url, show_unsub=show_unsub, logo_available=logo_available
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
    """The shared harvest-agent From used by outreach sends (and as the OTP
    fallback when OTP_FROM_EMAIL is blank), as a Mailjet ``{"Email","Name"}``
    object.

    Preferred: the explicit ``SMTP_SENDER_MAIL`` (the From address) + ``SMTP_ENVELOPE_NAME``
    (the display name, e.g. "JOB HARVEST AGENT"). This is required for Brevo, whose SMTP
    login (``SMTP_USERNAME``) is NOT a sendable From — mail must come from a verified sender.

    Fallback (when SMTP_SENDER_MAIL is unset): the legacy behavior — a real
    ``SMTP_FROM_EMAIL`` used as-is, else its bare display name shown while sending from the
    authenticated mailbox (``SMTP_USERNAME``). The From address must always be a real,
    provider-validated address, never a bare display name (that's what caused the
    "'JOB' is an invalid email address" 400)."""
    sender_mail = (settings.smtp_sender_mail or "").strip()
    if sender_mail:
        envelope_name = (settings.smtp_envelope_name or "").strip()
        out = {"Email": sender_mail}
        if envelope_name:
            out["Name"] = envelope_name
        return out

    configured = (settings.smtp_from_email or "").strip()
    username = (settings.smtp_username or "").strip()
    if "@" in configured:
        name, addr = parseaddr(configured)
        return {"Email": addr or username, "Name": f"SS - {configured}"}
    # Bare display name (or empty) → send from the authenticated mailbox and show
    # the display name as the sender name.
    return {"Email": username , "Name": f"SS - {configured}"}


def _otp_identity_from(settings: Settings) -> dict:
    """The From identity for the OTP (login) email — a dedicated no-reply sender
    ("Login OTP" <no-reply@sightspectrum.com> by default, OTP_FROM_EMAIL /
    OTP_FROM_NAME to override) so the login mail no longer shares the outreach
    identity. Falls back to the shared identity when OTP_FROM_EMAIL is blank."""
    otp_mail = (settings.otp_from_email or "").strip()
    if not otp_mail:
        return _shared_identity_from(settings)
    out = {"Email": otp_mail}
    otp_name = (settings.otp_from_name or "").strip()
    if otp_name:
        out["Name"] = otp_name
    return out


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


def _mailjet_message_ref(result: dict | None) -> str | None:
    """The identifier Mailjet assigned to a sent message, pulled from a v3.1 send
    result. Prefers the per-recipient ``MessageUUID`` (a stable string), falling
    back to the numeric ``MessageID``. Returns None when neither is present."""
    if not isinstance(result, dict):
        return None
    to = result.get("To") or []
    first = to[0] if to and isinstance(to[0], dict) else {}
    ref = first.get("MessageUUID") or first.get("MessageID")
    return str(ref) if ref else None


class EmailSender:
    """Email transport (Mailjet API or SMTP relay, per ``settings.email_provider``).
    AuthService only ever calls ``send_otp``; the outreach and harvest-report flows call
    ``send_email_with_attachments``. Both build a Mailjet-shaped message dict and hand it
    to ``_dispatch``, which routes to the configured transport."""

    _MAILJET_URL = "https://api.mailjet.com/v3.1/send"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send_otp(self, recipient: str, otp: str) -> None:
        settings = self._settings
        log = logger.bind(recipient=recipient)
        log.debug("otp_email_build_start")
        logo_bytes = _load_logo_bytes()

        # multipart/alternative: TextPart is the plain-text fallback, HTMLPart the
        # styled body. From is the dedicated login identity ("Login OTP"
        # <no-reply@…>), not the shared outreach/report one.
        message: dict = {
            "From": _otp_identity_from(settings),
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

        await self._dispatch(message, log)
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
        is_automation: bool = False,
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
        open + click tracking.

        `is_automation` (end-of-harvest auto-outreach only) appends AUTOMATION_CONTACT_BLOCK
        — a "reach out to us" name/phone line — directly below the body's closing
        website/deck line (and above the unsubscribe footer), in both the plain-text
        and HTML parts. Manual sends leave it False, so their body is unchanged."""
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

        # A stable identifier to persist for follow-up threading. Mailjet assigns
        # the real Message-ID and returns it in the send response (captured below);
        # this locally-generated value is only a fallback if that's ever absent.
        # Derive the domain from the configured sender so the fallback looks
        # legitimate. NOTE: Mailjet rejects a custom Message-ID in the "Headers"
        # collection (send-0011), so it is never stamped on the outgoing message.
        sender_addr = parseaddr(settings.smtp_from_email or "")[1]
        msgid_domain = sender_addr.split("@")[-1] if "@" in sender_addr else None
        message_id = make_msgid(domain=msgid_domain) if msgid_domain else make_msgid()

        # Automated outreach only: append the desk contact block below the body's
        # closing website/deck line. Everything downstream (unsubscribe footer, HTML
        # render) then flows from this augmented body, so the line lands under the
        # website link in both parts. Manual sends leave `body` untouched.
        body_for_render = apply_automation_contact_block(body) if is_automation else body

        # Outreach only (custom_id set): a plain, link-free unsubscribe message. We no
        # longer host our own unsubscribe URL — Mailjet owns the opt-out mechanism and
        # fires an `unsub` webhook event that the suppression workflow reacts to. It's
        # placed ABOVE the sign-off so the Regards signature stays the last thing in the
        # email; `body` stays un-mutated (the message goes only into `text_body`).
        text_body = (
            _insert_above_signoff(body_for_render, "Not interested? Reply to this email to unsubscribe.")
            if custom_id else body_for_render
        )

        message: dict = {
            "From": sender,
            "To": [{"Email": r} for r in recipients],
            "Subject": subject,
            "TextPart": text_body,
        }
        # Reply-To: the salesperson's own address, plus any configured EXTRA
        # Reply-To addresses on outreach sends (from_email set). The extras are
        # NEVER recipients of this email — a recruiter's reply reaches them only
        # because their mail client addresses every Reply-To when they hit Reply
        # (RFC 5322 permits multiple Reply-To mailboxes). The harvest report has no
        # from_email, so it never gets the extras.
        reply_addrs: list[str] = []
        primary = (reply_to or from_email or "").strip()
        if primary:
            reply_addrs.append(primary)
        if from_email:
            for a in settings.outreach_extra_reply_to_recipients:
                if a and a.lower() not in {r.lower() for r in reply_addrs}:
                    reply_addrs.append(a)
        if len(reply_addrs) == 1:
            message["ReplyTo"] = {"Email": reply_addrs[0]}
        elif len(reply_addrs) > 1:
            # Mailjet's ReplyTo field is single-valued, so carry the full list as a
            # Reply-To header instead (Mailjet passes `Headers` through to the message;
            # the SMTP converter reads it too — see _mailjet_dict_to_email_message).
            message["Headers"] = {**(message.get("Headers") or {}), "Reply-To": ", ".join(reply_addrs)}

        # HTML alternative: a pre-rendered report body wins; otherwise derive the
        # outreach HTML from `body` (augmented with the automation contact block when
        # applicable). With neither, the mail is text-only.
        if html_body is not None:
            message["HTMLPart"] = html_body
        elif as_html:
            # Embed the logo inline (CID) so the signature card renders it without a
            # remote fetch. The SMTP converter attaches it WITHOUT a filename so mail
            # clients render it in the card but don't list it under "Attachments".
            logo_bytes = _load_logo_bytes()
            message["HTMLPart"] = render_outreach_email_html(
                body_for_render, job_title, job_url,
                show_unsub=bool(custom_id), logo_available=logo_bytes is not None,
            )
            if logo_bytes is not None:
                message.setdefault("InlinedAttachments", []).append({
                    "ContentType": "image/jpeg",
                    "Filename": "sight_spectrum_logo.jpg",
                    "ContentID": LOGO_CID,
                    "Base64Content": base64.b64encode(logo_bytes).decode("ascii"),
                })

        attachments = _build_attachments(paths, blobs, log)
        if attachments:
            message["Attachments"] = attachments

        # Outreach only: correlate delivery events to the send row. Open/click tracking
        # is gated behind OUTREACH_TRACK_ENGAGEMENT (default off) — the tracking pixel +
        # link rewriting it enables are strong Gmail-Promotions signals, and delivery
        # events (sent/bounce/blocked/spam/unsub) don't depend on it. Unsubscribe is now
        # Mailjet-managed, so we no longer advertise a self-hosted List-Unsubscribe here.
        if custom_id:
            message["CustomID"] = custom_id
            track = "enabled" if settings.outreach_track_engagement else "disabled"
            message["TrackOpens"] = track
            message["TrackClicks"] = track

        # `message_id` is the RFC Message-ID the SMTP path stamps + returns; the Mailjet
        # path returns its own MessageUUID/MessageID. Either way, fall back to the
        # locally-generated id if the transport yields nothing.
        ref = await self._dispatch(message, log, fallback_message_id=message_id)
        log.info("email_with_attachments_sent")
        return ref or message_id

    async def _dispatch(self, message: dict, log=None, fallback_message_id: str | None = None) -> str | None:
        """Send `message` (a Mailjet-shaped dict) via the configured transport and return
        a provider message reference (or None). Mailjet → its MessageUUID/MessageID; SMTP
        → the RFC Message-ID we stamp. Both raise on failure so the caller records
        status="failed"."""
        if (self._settings.email_provider or "mailjet").lower() == "smtp":
            return await self._send_via_smtp(message, log, fallback_message_id)
        result = await self._send_via_mailjet(message, log)
        return _mailjet_message_ref(result)

    async def _send_via_mailjet(self, message: dict, log=None) -> dict:
        """POST a single v3.1 message to the Mailjet Send API, retrying transient
        failures. Raises on a permanent transport error, a retryable error that
        outlives its attempts, or a non-success message status so callers can record
        the failure.

        A single flaky TLS handshake to api.mailjet.com used to fail a send outright;
        connect/read/write/timeout errors and retryable HTTP statuses (429/5xx) are now
        retried up to ``mailjet_max_attempts`` times with exponential backoff
        (``mailjet_retry_backoff_seconds``, doubling each attempt). On final failure the
        raised error carries a descriptive message (the bare ``ConnectError`` stringifies
        to ""), so the caller stores a meaningful ``error_message`` instead of a blank."""
        log = log or logger
        settings = self._settings
        if not settings.mailjet_api_key or not settings.mailjet_secret_key:
            raise RuntimeError(
                "Mailjet credentials are not configured "
                "(set MJ_APIKEY_PUBLIC / MJ_APIKEY_PRIVATE)."
            )
        payload = {"Messages": [message]}
        attempts = max(1, settings.mailjet_max_attempts)
        backoff = max(0.0, settings.mailjet_retry_backoff_seconds)
        log.debug("mailjet_sending", to=message.get("To"), max_attempts=attempts)

        for attempt in range(1, attempts + 1):
            retry_in = backoff * (2 ** (attempt - 1))  # 0.5s → 1s → 2s …
            try:
                async with httpx.AsyncClient(timeout=settings.mailjet_timeout_seconds) as client:
                    resp = await client.post(
                        self._MAILJET_URL,
                        json=payload,
                        auth=(settings.mailjet_api_key, settings.mailjet_secret_key),
                    )
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                body = exc.response.text[:500]
                if status_code in _RETRYABLE_STATUS and attempt < attempts:
                    log.warning(
                        "mailjet_http_error_retrying",
                        status=status_code, attempt=attempt, max_attempts=attempts,
                        retry_in=retry_in, body=body,
                    )
                    await asyncio.sleep(retry_in)
                    continue
                log.error("mailjet_http_error", status=status_code, attempt=attempt, body=body)
                raise
            except _RETRYABLE_TRANSPORT_ERRORS as exc:
                # These stringify to "" (e.g. ConnectError(BrokenResourceError())) — log
                # and, on exhaustion, raise with repr() so the failure is diagnosable.
                if attempt < attempts:
                    log.warning(
                        "mailjet_transport_error_retrying",
                        error=repr(exc), error_type=type(exc).__name__,
                        attempt=attempt, max_attempts=attempts, retry_in=retry_in,
                    )
                    await asyncio.sleep(retry_in)
                    continue
                log.error(
                    "mailjet_transport_error",
                    error=repr(exc), error_type=type(exc).__name__, attempt=attempt,
                )
                raise RuntimeError(
                    f"Mailjet transport error after {attempts} attempt(s): "
                    f"{type(exc).__name__}: {exc!r}"
                ) from exc
            except httpx.HTTPError as exc:
                # Non-retryable transport error (e.g. an unsupported protocol/URL issue).
                log.error(
                    "mailjet_transport_error",
                    error=repr(exc), error_type=type(exc).__name__, attempt=attempt,
                )
                raise RuntimeError(
                    f"Mailjet transport error: {type(exc).__name__}: {exc!r}"
                ) from exc

            data = resp.json()
            result = (data.get("Messages") or [{}])[0]
            if str(result.get("Status", "")).lower() != "success":
                # A rejection is a permanent content/recipient problem — not retried.
                log.error("mailjet_message_rejected", result=result)
                raise RuntimeError(f"Mailjet rejected the message: {result}")
            if attempt > 1:
                log.info("mailjet_send_ok", attempt=attempt, recovered=True)
            else:
                log.debug("mailjet_send_ok")
            return result

        # Unreachable: the loop either returns on success or raises on the final
        # attempt. Guards against a future edit dropping the terminal raise.
        raise RuntimeError("Mailjet send exhausted all attempts without a result.")

    async def _send_via_smtp(self, message: dict, log=None, fallback_message_id: str | None = None) -> str | None:
        """Send the Mailjet-shaped `message` over a plain SMTP relay (Brevo), retrying
        transient failures. Translates the dict into a stdlib ``EmailMessage`` and sends
        it on a worker thread (``smtplib`` is blocking). Returns the RFC ``Message-ID``
        stamped on the outgoing mail (persisted as provider_message_id); raises with a
        descriptive message on permanent failure or after exhausting retries.

        ``CustomID`` rides along as the ``X-Mailin-custom`` header so Brevo echoes the
        outreach-row id back on its transactional webhooks (the correlation key). Open/click
        tracking is Brevo-side and on by default, so the Mailjet-only ``TrackOpens``/
        ``TrackClicks`` keys are simply ignored here."""
        log = log or logger
        settings = self._settings
        if not settings.smtp_host:
            raise RuntimeError(
                "SMTP host is not configured (set SMTP_HOST for email_provider='smtp')."
            )
        # Stamp a Message-ID we control (SMTP relays preserve it) so provider_message_id is
        # populated for threading; derive the domain from the From address.
        from_addr = (message.get("From") or {}).get("Email") or ""
        domain = from_addr.split("@")[-1] if "@" in from_addr else None
        message_id = fallback_message_id or (make_msgid(domain=domain) if domain else make_msgid())
        email_msg, recipients = _mailjet_dict_to_email_message(message, message_id)

        # Reuse the same retry knobs as the Mailjet path (attempts + exponential backoff).
        attempts = max(1, settings.mailjet_max_attempts)
        backoff = max(0.0, settings.mailjet_retry_backoff_seconds)
        log.debug("smtp_sending", host=settings.smtp_host, port=settings.smtp_port,
                  to=recipients, max_attempts=attempts)

        for attempt in range(1, attempts + 1):
            retry_in = backoff * (2 ** (attempt - 1))  # 0.5s → 1s → 2s …
            try:
                await asyncio.to_thread(self._smtp_send_sync, email_msg)
            except _RETRYABLE_SMTP_ERRORS as exc:
                if attempt < attempts:
                    log.warning(
                        "smtp_transport_error_retrying",
                        error=repr(exc), error_type=type(exc).__name__,
                        attempt=attempt, max_attempts=attempts, retry_in=retry_in,
                    )
                    await asyncio.sleep(retry_in)
                    continue
                log.error(
                    "smtp_transport_error",
                    error=repr(exc), error_type=type(exc).__name__, attempt=attempt,
                )
                raise RuntimeError(
                    f"SMTP transport error after {attempts} attempt(s): "
                    f"{type(exc).__name__}: {exc!r}"
                ) from exc
            except smtplib.SMTPException as exc:
                # Auth failure / recipients refused / message rejected — permanent, no retry.
                log.error(
                    "smtp_send_rejected",
                    error=repr(exc), error_type=type(exc).__name__, attempt=attempt,
                )
                raise RuntimeError(
                    f"SMTP send rejected: {type(exc).__name__}: {exc!r}"
                ) from exc

            if attempt > 1:
                log.info("smtp_send_ok", attempt=attempt, recovered=True)
            else:
                log.debug("smtp_send_ok")
            return message_id

        # Unreachable — mirrors the Mailjet loop's terminal guard.
        raise RuntimeError("SMTP send exhausted all attempts without a result.")

    def _smtp_send_sync(self, email_msg: EmailMessage) -> None:
        """Blocking SMTP send (runs on a worker thread). Envelope sender/recipients are
        taken from the message's From/To headers by ``send_message``."""
        settings = self._settings
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds) as server:
            if settings.smtp_use_tls:
                server.starttls()
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(email_msg)


def _guess_attachment_type(path: Path) -> tuple[str, str]:
    content_type, _ = mimetypes.guess_type(path.name)
    if content_type is None:
        return "application", "octet-stream"
    maintype, subtype = content_type.split("/", 1)
    return maintype, subtype


def _split_content_type(ctype: str | None) -> tuple[str, str]:
    """Split a ``"maintype/subtype"`` string into a pair, defaulting to a generic binary
    type — for turning a Mailjet-shaped attachment's ContentType back into EmailMessage's
    ``maintype``/``subtype`` args."""
    if ctype and "/" in ctype:
        maintype, subtype = ctype.split("/", 1)
        return maintype, subtype
    return "application", "octet-stream"


def _mailjet_dict_to_email_message(message: dict, message_id: str) -> tuple[EmailMessage, list[str]]:
    """Translate the Mailjet-shaped message dict (the one both send_* methods build) into a
    stdlib ``EmailMessage`` for the SMTP transport. Returns ``(email_message, recipients)``.

    Mapping: From/To/ReplyTo objects → headers; TextPart → plain body; HTMLPart →
    multipart/alternative HTML part; InlinedAttachments → CID-related images on the HTML
    part (so the OTP logo renders inline); Attachments → regular attachments; CustomID →
    the ``X-Mailin-custom`` header (Brevo's webhook correlation key). Mailjet-only tracking
    keys (TrackOpens/TrackClicks) are ignored — Brevo tracks by default."""
    msg = EmailMessage()

    frm = message.get("From") or {}
    from_email = (frm.get("Email") or "").strip()
    from_name = (frm.get("Name") or "").strip()
    msg["From"] = formataddr((from_name, from_email)) if from_name else from_email

    recipients = [(m.get("Email")).strip() for m in (message.get("To") or []) if m.get("Email")]
    msg["To"] = ", ".join(recipients)

    # Reply-To: a multi-address send carries the full comma-separated list in
    # Headers["Reply-To"] (Mailjet's ReplyTo object is single-valued); a single-address
    # send uses the ReplyTo object. Prefer the header list when present.
    reply_to = (message.get("Headers") or {}).get("Reply-To") or (message.get("ReplyTo") or {}).get("Email")
    if reply_to:
        msg["Reply-To"] = reply_to

    msg["Subject"] = message.get("Subject") or ""
    if message_id:
        msg["Message-ID"] = message_id
    custom_id = message.get("CustomID")
    if custom_id:
        # Brevo echoes this header verbatim on its transactional webhooks, letting the
        # brevo-events endpoint match a delivery/open/click event back to the send row.
        msg["X-Mailin-custom"] = str(custom_id)

    # Bodies: plain-text is the required root; HTML (if any) is the alternative.
    msg.set_content(message.get("TextPart") or "")
    html = message.get("HTMLPart")
    if html:
        msg.add_alternative(html, subtype="html")

    # Inline images (the logo) → related to the HTML part so they render inline.
    # Deliberately NO filename: a named inline part is what makes mail clients list
    # it under "Attachments" even though it renders in the body. Unnamed + cid +
    # Content-Disposition: inline keeps it a pure body resource. (The Filename in
    # the message dict is only for the Mailjet API path, which requires it.)
    inlined = message.get("InlinedAttachments") or []
    if inlined and html:
        html_part = msg.get_payload()[-1]
        for att in inlined:
            data = base64.b64decode(att.get("Base64Content") or "")
            maintype, subtype = _split_content_type(att.get("ContentType"))
            cid = att.get("ContentID") or ""
            html_part.add_related(data, maintype=maintype, subtype=subtype, cid=f"<{cid}>")

    # Regular file attachments (e.g. the harvest report) added after the alternative.
    for att in (message.get("Attachments") or []):
        data = base64.b64decode(att.get("Base64Content") or "")
        maintype, subtype = _split_content_type(att.get("ContentType"))
        msg.add_attachment(
            data, maintype=maintype, subtype=subtype,
            filename=att.get("Filename") or "attachment",
        )

    return msg, recipients
