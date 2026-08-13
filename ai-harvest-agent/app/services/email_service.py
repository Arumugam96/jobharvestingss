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
import mimetypes
import smtplib
from email.message import EmailMessage
from pathlib import Path

import structlog

from app.config import Settings

logger = structlog.get_logger(__name__)

OTP_EMAIL_SUBJECT = "Your Sightspectrum Login OTP"


def render_otp_email(otp: str, expiry_seconds: int) -> str:
    """Plain-text OTP email body. Edit this to restyle the email."""
    minutes = max(1, expiry_seconds // 60)
    return (
        "Hello,\n\n"
        "Your one-time password for signing in is:\n\n"
        f"{otp}\n\n"
        f"This OTP expires in {minutes} minute(s).\n\n"
        "If you did not request this OTP, please ignore this email.\n\n"
        "Regards,\n"
        "Sightspectrum"
    )


class EmailSender:
    """SMTP abstraction. AuthService only ever calls ``send_otp``."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send_otp(self, recipient: str, otp: str) -> None:
        settings = self._settings
        log = logger.bind(recipient=recipient)
        log.debug("otp_email_build_start")
        message = EmailMessage()
        message["Subject"] = OTP_EMAIL_SUBJECT
        message["From"] = settings.smtp_from_email
        message["To"] = recipient
        message.set_content(render_otp_email(otp, settings.otp_expiry_seconds))

        await asyncio.to_thread(self._send_sync, message, log)
        log.info("otp_email_sent")

    async def send_email_with_attachments(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        attachment_paths: list[str] | None = None,
        attachment_blobs: list[tuple[str, bytes]] | None = None,
    ) -> None:
        """Generic SMTP send with attachments — same transport/credentials as
        send_otp. Attachments come as file paths and/or as in-memory
        (filename, bytes) blobs; the harvest report is generated from the DB
        in memory and attached as a blob (no result file is written to disk)."""
        paths = attachment_paths or []
        blobs = attachment_blobs or []
        log = logger.bind(recipients=recipients, subject=subject, attachments=len(paths) + len(blobs))
        log.debug("email_with_attachments_start")
        await asyncio.to_thread(
            self._send_with_attachments_sync, recipients, subject, body, paths, blobs, log
        )
        log.info("email_with_attachments_sent")

    def _send_with_attachments_sync(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        attachment_paths: list[str],
        attachment_blobs: list[tuple[str, bytes]],
        log,
    ) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self._settings.smtp_from_email
        message["To"] = ", ".join(recipients)
        message.set_content(body)

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

        self._send_sync(message, log)

    def _send_sync(self, message: EmailMessage, log=None) -> None:
        log = log or logger
        settings = self._settings
        log.debug(
            "smtp_connecting",
            host=settings.smtp_host,
            port=settings.smtp_port,
            use_tls=settings.smtp_use_tls,
        )
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
                log.debug("smtp_connected")
                if settings.smtp_use_tls:
                    server.starttls()
                    log.debug("smtp_starttls_ok")
                if settings.smtp_username:
                    log.debug("smtp_login_attempt", username=settings.smtp_username)
                    server.login(settings.smtp_username, settings.smtp_password)
                    log.debug("smtp_login_ok")
                log.debug("smtp_sending_message", to=message["To"])
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
